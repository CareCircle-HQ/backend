"""Admin > Reports: management-only CSV exports.

Each report is a management-gated endpoint that streams a downloadable CSV. The
first report exports members carrying a given lead source (e.g. the "Hyphen Met"
CallTools queue) within an optional Client.created_at date range.
"""

import csv
import functools
import operator
import re
from datetime import datetime

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    AddressType,
    Agent,
    Case,
    CaseStatus,
    CaseType,
    Client,
    DeliveryCadence,
    DietaryRestriction,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    MemberStatus,
    ProductTypeKind,
    ReportExport,
    ReportExportStatus,
    ServiceAuthorizationStatus,
    SocialCareCoverageStatus,
    UniteUsAgent,
)
from ..services.catalog import product_type_kind_for_name
from .base import PortalAPIView, current_agent
from .serializers import (
    _assessment_eligible,
    active_enrollment,
    active_member_profile,
    internal_service_case,
    medicaid_member_id,
    member_out_of_orbit,
    member_out_of_range,
)
from .views_members import _parse_date


def _lead_source_label_map():
    """Best-effort CallTools queue id -> name map, so a stored lead_source that is
    a queue id (e.g. "5851") resolves to its friendly label ("Hyphen Met") in the
    export. Never lets a CallTools hiccup break the report."""
    mapping = {}
    try:
        from ..integrations.calltools import campaigns as ct_campaigns
        from ..integrations.calltools import config as ct_config
        from ..integrations.calltools import queues as ct_queues

        if ct_config.is_enabled():
            ct_options = []
            try:
                ct_options += ct_queues.list_queue_options()
            except Exception:
                pass
            try:
                ct_options += ct_campaigns.list_campaign_options()
            except Exception:
                pass
            for q in ct_options:
                qid = str(q.get("id") or "").strip()
                name = (q.get("name") or "").strip()
                if qid and name:
                    mapping.setdefault(qid, name)
    except Exception:
        pass
    return mapping


class MembersByLeadSourceReportView(PortalAPIView):
    """Management-only CSV export of members carrying a given lead source.

    Query params:
        lead_source   -- required; one OR MORE lead sources (repeat the param, or
                         pass a comma-separated list). Each is matched
                         case-insensitively against Client.lead_source; a member
                         is included if it matches ANY selected source. Values
                         come from the same dropdown the Members page uses.
        created_from  -- optional inclusive lower bound on Client.created_at (date).
        created_to    -- optional inclusive upper bound on Client.created_at (date).
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        # Accept repeated ?lead_source= params AND a comma-separated fallback.
        lead_sources = [v.strip() for v in request.query_params.getlist("lead_source") if v.strip()]
        if not lead_sources:
            lead_sources = [
                v.strip()
                for v in (request.query_params.get("lead_source") or "").split(",")
                if v.strip()
            ]
        if not lead_sources:
            return Response({"detail": "At least one lead_source is required."}, status=400)

        from .report_exports import (
            default_filename, members_by_lead_source_rows, stream_csv_response,
        )

        params = {
            "lead_sources": lead_sources,
            "created_from": request.query_params.get("created_from"),
            "created_to": request.query_params.get("created_to"),
        }
        return stream_csv_response(
            members_by_lead_source_rows(params),
            default_filename("members-by-lead-source"),
        )


def _enrollment_member_clients(enrollment):
    """Distinct member clients of an enrollment's household (incl. the primary).

    A verification applies to the whole household, so every household member is
    'waiting for verification'. De-duplicated by client id."""
    clients = {}
    household = enrollment.household
    if household is not None:
        for hm in household.members.all():
            if hm.client_id:
                clients[hm.client_id] = hm.client
    for mp in enrollment.member_profiles.all():
        if mp.client_id:
            clients.setdefault(mp.client_id, mp.client)
    if enrollment.client_id:
        clients.setdefault(enrollment.client_id, enrollment.client)
    return list(clients.values())


def _client_phone_numbers(client):
    """All phone numbers on file for a client (primary first), '; '-joined.
    Falls back to the single ``client_phone_number`` field when none are on
    the related phones table."""
    numbers = []
    for p in client.phones.all():
        raw = (p.raw or p.normalized or "").strip()
        if raw and raw not in numbers:
            numbers.append(raw)
    if not numbers and (client.client_phone_number or "").strip():
        numbers.append(client.client_phone_number.strip())
    return "; ".join(numbers)


class MembersPendingVerificationReportView(PortalAPIView):
    """Management-only CSV export of members genuinely awaiting verification
    (stage ``pending_verification`` and not already verified/served on a later
    enrollment), filtered by the governing internal-service case's created date.

    Columns: Client ID, First Name, Last Name, Phone Numbers, Case Created.

    Query params:
        created_from -- optional inclusive lower bound on the governing case's
                        created (date_opened) date.
        created_to   -- optional inclusive upper bound on the governing case's
                        created (date_opened) date.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        from .report_exports import (
            default_filename, members_pending_verification_rows, stream_csv_response,
        )

        params = {
            "created_from": request.query_params.get("created_from"),
            "created_to": request.query_params.get("created_to"),
        }
        return stream_csv_response(
            members_pending_verification_rows(params),
            default_filename("members-pending-verification"),
        )


class AllVerificationsReportView(PortalAPIView):
    """Management-only CSV export of every verification (one row per enrollment
    verification), with the key milestone dates.

    Columns: Member ID, Verification Requested (date), Verification Completed
    (date), Authorization Approved (date).

    Query params (optional, on the verification-requested date):
        requested_from / requested_to -- inclusive [from, to] bounds.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        from .report_exports import (
            all_verifications_rows, default_filename, stream_csv_response,
        )

        params = {
            "requested_from": request.query_params.get("requested_from"),
            "requested_to": request.query_params.get("requested_to"),
        }
        return stream_csv_response(
            all_verifications_rows(params), default_filename("all-verifications"),
        )


_CLOSED_CASE_STATUSES = (CaseStatus.CLOSED, CaseStatus.CANCELLED)


def _primary_phone(client):
    """A single best phone number for the member: the canonical
    ``client_phone_number`` if set, else the first number on the phones table."""
    number = (client.client_phone_number or "").strip()
    if number:
        return number
    for p in client.phones.all():
        raw = (p.raw or p.normalized or "").strip()
        if raw:
            return raw
    return ""


def _yn(value):
    return "Yes" if value else "No"


def _date_str(value):
    """ISO date string for a date/datetime, or "" when None.

    Aware datetimes are stored in UTC; convert to the project's local timezone
    before taking the calendar date so an evening EDT timestamp (e.g. 9:34 PM,
    01:34 UTC next day) doesn't roll forward a day in the export -- matching the
    CRM UI, which renders in local time.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date().isoformat()
    d = value.date() if hasattr(value, "date") else value
    return d.isoformat()


# Medicaid plan-name -> served-type classification, evaluated top-down (the more
# specific abbreviations/phrases win). Mirrors the ineligible-type detector in
# api.services.warnings so the export and the eligibility gate never drift.
_MEDICAID_TYPE_SPECS = (
    ("PMLTC", ("PMLTC", "Partial Managed Long Term Care")),
    ("MLTCP", ("MLTCP", "Managed Long Term Care Partial")),
    ("MLTC", ("MLTC", "Managed Long Term Care")),
    ("MAP", ("MAP", "Medicaid Advantage Plan")),
    ("FFS", ("FFS", "Fee For Service")),
)


def _medicaid_type_matchers():
    out = []
    for label, tokens in _MEDICAID_TYPE_SPECS:
        parts = [r"\s+".join(re.escape(w) for w in t.split()) for t in tokens]
        out.append((label, re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)))
    return out


_MEDICAID_TYPE_MATCHERS = _medicaid_type_matchers()


def _medicaid_insurance(client):
    """The member's representative Medicaid plan: an ACTIVE one (primary
    preferred), else the primary/first Medicaid plan on file, else None."""
    meds = [i for i in client.insurances.all() if (i.plan_type or "").lower() == "medicaid"]
    if not meds:
        return None
    active = [i for i in meds if (i.status or "").lower() == "active"]
    pool = active or meds
    return next((i for i in pool if i.is_primary), pool[0])


def _medicaid_type_label(plan_name):
    """Classify a Medicaid plan name as PMLTC/MLTCP/MLTC/MAP/FFS, else 'OK'.
    Blank plan name -> ''."""
    name = (plan_name or "").strip()
    if not name:
        return ""
    for label, matcher in _MEDICAID_TYPE_MATCHERS:
        if matcher.search(name):
            return label
    return "OK"


def _social_care_coverage(client):
    """The member's representative social-care coverage: an ENROLLED one, else
    the latest on file (rows are ordered -enrolled_at), else None."""
    covs = list(client.social_care_coverages.all())
    if not covs:
        return None
    enrolled = [c for c in covs if c.status == SocialCareCoverageStatus.ENROLLED]
    return (enrolled or covs)[0]


_SCC_STATUS_LABELS = dict(SocialCareCoverageStatus.choices)
_DIETARY_LABELS = dict(DietaryRestriction.choices)
_ALLERGY_LABELS = dict(FoodAllergy.choices)
_CADENCE_LABELS = dict(DeliveryCadence.choices)
_ENROLLMENT_STAGE_LABELS = dict(EnrollmentStage.choices)
_PRODUCT_KIND_LABELS = dict(ProductTypeKind.choices)


def _meals_or_boxes(program_name):
    """Classify a case as a Meals or Boxes case from its program name keywords
    (Meals wins; the box family also covers voucher / produce-prescription /
    pantry names). '' when the name carries no product keyword."""
    kind = product_type_kind_for_name(program_name)
    return _PRODUCT_KIND_LABELS.get(kind, "") if kind else ""


def _dietary_restrictions(profile):
    """'; '-joined dietary restrictions + food allergies + free-text notes for a
    member's dietary profile (excludes the 'none' sentinel). '' when none."""
    if profile is None:
        return ""
    parts = []
    for code in (profile.dietary_restrictions or []):
        if code and code != "none":
            parts.append(_DIETARY_LABELS.get(code, code))
    for code in (profile.food_allergies or []):
        if code and code != "none":
            parts.append(_ALLERGY_LABELS.get(code, code))
    other = (profile.other_dietary_restrictions or "").strip()
    if other:
        parts.append(other)
    seen, uniq = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return "; ".join(uniq)


def _cadence_label(profile, enrollment):
    """The member's delivery cadence label (household-level). Prefers the
    member's own delivery schedule, else the household's first. '' when none."""
    if enrollment is None:
        return ""
    schedules = list(enrollment.delivery_schedules.all())
    if not schedules:
        return ""
    own = None
    if profile is not None:
        own = next((s for s in schedules if s.member_profile_id == profile.pk), None)
    sched = own or schedules[0]
    code = (sched.delivery_days_cadence or "").strip()
    return _CADENCE_LABELS.get(code, code)


def _currently_servicing(enrollment):
    """The member's live enrollment stage label (Active, Kitchen Assignment,
    Pending Verification, ...); '' when there is no active enrollment."""
    if enrollment is None:
        return ""
    return _ENROLLMENT_STAGE_LABELS.get(enrollment.stage, enrollment.stage or "")


def _out_of_orbit_reason(enrollment, profile):
    """Recompute the member's out-of-orbit reason from the meal rule + assigned
    kitchen, WITHOUT writing to the DB. '' when the member is serviceable."""
    if profile is None:
        return ""
    from ..services.meal_rules import reconcile_member_kitchen_output

    try:
        kitchen = enrollment.kitchen if enrollment is not None else None
        _out, _became, reason = reconcile_member_kitchen_output(
            profile, kitchen, save=False
        )
        return reason or ""
    except Exception:  # pragma: no cover - defensive; a report must never 500
        return ""


def _member_household(client):
    """The client's household (via their membership), or None."""
    hm = getattr(client, "household_membership", None)
    return hm.household if hm is not None else None


def _household_primary_member_id(client):
    """Client ID of the primary member of the client's household. A lone member
    (no household row) is their own primary, so fall back to their own id."""
    household = _member_household(client)
    if household is not None:
        primary = next((m for m in household.members.all() if m.is_primary), None)
        if primary and primary.client_id:
            return str(primary.client_id)
    return str(client.client_id)


def _household_member_count(client):
    """Number of members in the client's household (incl. the primary); 1 when
    the client isn't attached to a household (just themselves)."""
    household = _member_household(client)
    if household is None:
        return 1
    return len(household.members.all()) or 1


def _current_address(client):
    """The client's representative address: a 'current' type preferred, then
    'home', else the first on file. None when there is none."""
    addrs = list(client.addresses.all())
    if not addrs:
        return None
    for want in (AddressType.CURRENT, AddressType.HOME):
        match = next((a for a in addrs if a.type == want), None)
        if match is not None:
            return match
    return addrs[0]


class AllMembersReportView(PortalAPIView):
    """Management-only CSV export of every member (Client), one row per member.

    Columns follow the Reports "All Members" spec, grouped: Identification
    (Household Primary Member ID, Member ID, Medicaid ID (active), Member Name,
    DOB), Contact & Address (Phone Number, Street Address, Apt, City, State,
    Zip), Household (Total members in household), Program & Case Status (Internal
    Service Program Name, Is there Screening/Eligibility/Navigation/Internal
    Service Case, Client Eligibility, Currently servicing, Cadence, Facility, Out
    of Orbit? + reason, Out of Range, Authorized amount for the open internal
    service case), Lead & Enrollment (Lead Source in CRM, Enrollment Platform),
    Insurance/Medicaid (Medicaid Plan, Medicaid Type, Insurance Effective/
    Expiration Date), Social Care Coverage (Status, Expiration Date), plus Menu
    Type + Dietary Restrictions appended.

    Query params (both optional; omit both to export every member):
        created_from -- inclusive lower bound on the member's created date.
        created_to   -- inclusive upper bound on the member's created date.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        from .report_exports import (
            all_members_rows, default_filename, stream_csv_response,
        )

        params = {
            "created_from": request.query_params.get("created_from"),
            "created_to": request.query_params.get("created_to"),
        }
        # Streamed (stream_csv_response): starts sending immediately + keeps
        # memory bounded. The heavy full-member export is normally run via the
        # background export flow (POST /reports/exports/); this sync path is the
        # no-S3 fallback + small filtered pulls.
        return stream_csv_response(
            all_members_rows(params), default_filename("all-members"),
        )


_WEEKDAY_ABBR = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}


def _case_enrollment(case):
    """The enrollment governing a case's delivery, preferring a live one over a
    disregarded/cancelled row. ``None`` when the case has no enrollment."""
    enrs = list(case.enrollments.all())
    if not enrs:
        return None
    live = [e for e in enrs if e.stage not in ("disregarded", "cancelled")]
    return (live or enrs)[0]


def _enrollment_kitchen(enrollment):
    """The kitchen assigned to the enrollment's household; '' when unassigned."""
    if enrollment is not None and enrollment.kitchen_id:
        return enrollment.kitchen.name or ""
    return ""


def _enrollment_cadence(enrollment):
    """The enrollment's delivery cadence label. Prefers the delivery schedule's
    cadence code (e.g. 'Mon/Thu'), falling back to the raw delivery weekdays."""
    label = _cadence_label(None, enrollment)
    if label:
        return label
    if enrollment is not None and enrollment.delivery_weekdays:
        return "/".join(
            _WEEKDAY_ABBR.get(w, w) for w in enrollment.delivery_weekdays
        )
    return ""


class CasesReportView(PortalAPIView):
    """Management-only CSV export of cases, one row per case, optionally limited
    to a case-created (``date_opened``) date range.

    Columns: Client ID, Case ID, Member Name, Member Phone Number, Team of Case
    Creator, Case Created By Name, Case Created Date, Case Closed Date, Case
    Status, Originating Provider Name, Provider Name, Program Name, Is Program
    Household?, Case Type, Meals/Boxes, Kitchen, Cadence, Primary Worker Name,
    Care Coordinator, Service Authorization Status, Service Authorization End
    Date.

    ``Is Program Household?`` is computed LIVE from the word "Household" in the
    program name (mirrors ``derive_household_type``) rather than reading the
    stored ``household_type``, so the export is correct even for rows written
    before the classification rule was unified. ``Meals/Boxes`` is derived from
    the program name's product keyword.

    Query params:
        created_from -- optional inclusive lower bound on the case's created
                        (``date_opened``) date.
        created_to   -- optional inclusive upper bound on the case's created
                        (``date_opened``) date.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        from .report_exports import cases_rows, default_filename, stream_csv_response

        params = {
            "created_from": request.query_params.get("created_from"),
            "created_to": request.query_params.get("created_to"),
        }
        return stream_csv_response(cases_rows(params), default_filename("cases"))


class MembersForPurchaseOrderReportView(PortalAPIView):
    """Management-only CSV of active members scheduled to land on a Purchase
    Order for a given delivery date (or the current week).

    Source of truth is the dated delivery calendar (:class:`OrderSchedule`) --
    the exact set PO generation aggregates -- filtered to still-scheduled
    deliveries for servable members. One row per member per delivery.

    Columns: Delivery Date, HouseholdGroup, PrimaryMemberID, Client ID,
    PrimaryHousehold (is-head flag), Quantity, Delivery Address, Menu Type,
    Meal Type (Meal/Box), Kitchen, Cadence, Case ID, Case Status, Case
    Authorization, Member Status.

    Query params:
        scope    -- "date" (default) or "week".
        date     -- required when scope=date; must be a FUTURE date (YYYY-MM-DD).
        cadence  -- optional (scope=date only): mon_thu | tue_fri | once_a_week;
                    keeps only deliveries whose weekday belongs to that cadence.
        scope=week exports the entire current Mon-Sun week for every cadence.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        from .report_exports import (
            default_filename, members_for_po_rows, stream_csv_response,
        )

        today = timezone.localdate()
        scope = (request.query_params.get("scope") or "date").lower()
        params = {"scope": scope}
        if scope != "week":
            d = _parse_date(request.query_params.get("date"))
            if d is None:
                return Response({"detail": "A valid date is required."}, status=400)
            if d <= today:
                return Response({"detail": "Date must be in the future."}, status=400)
            params["date"] = request.query_params.get("date")
            params["cadence"] = (request.query_params.get("cadence") or "").strip()
        return stream_csv_response(
            members_for_po_rows(params), default_filename("members-for-po"),
        )


class MembersNotServedReportView(PortalAPIView):
    """Management-only CSV of members who HAVE an internal-service case (Household
    or Individual program) but are NOT currently being served on any Purchase
    Order -- i.e. they have no scheduled delivery in the calendar -- regardless
    of the case's status.

    Columns: Client ID, Case ID, Case Status, Case Type, Case Authorization,
    Program Name, Meals/Boxes, Is Part of a Household, Household Group, Primary
    Member ID, Is Primary, Full Name, Member Stage, Kitchen, Cadence, Menu Type,
    Out of Orbit, Out of Range, On Hold, Cancelled. The status flags explain WHY
    a member isn't being served (out-of-orbit/out-of-range/paused/ended); all can
    be No when the member simply never reached a Purchase Order (e.g. pending
    verification). Kitchen / Cadence / Menu Type are blank until assigned.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)
        from .report_exports import (
            default_filename, members_not_served_rows, stream_csv_response,
        )

        return stream_csv_response(
            members_not_served_rows({}), default_filename("members-not-served"),
        )


class UniteUsAgentsReportView(PortalAPIView):
    """Management-only CSV of every Unite Us agent (the Unite NYC / SCN platform
    users on the allowlist, sourced from the Unite Us users export).

    Columns: Unite Us user_id, Full Name, Email, Team, Status.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        from .report_exports import (
            default_filename, stream_csv_response, unite_us_agents_rows,
        )

        return stream_csv_response(
            unite_us_agents_rows({}), default_filename("unite-us-agents"),
        )


# ===========================================================================
# Background report exports (POST start job -> Celery -> S3; GET poll status)
# ===========================================================================
def _is_management(agent):
    return bool(
        agent and (agent.group == "Management" or getattr(agent, "is_manager", False))
    )


def _report_export_payload(export):
    """Serialize a ReportExport for the UI. When completed, include a short-lived
    presigned download URL."""
    from ..services import import_storage

    download_url = ""
    if (
        export.status == ReportExportStatus.COMPLETED
        and export.file_key
        and import_storage.s3_enabled()
    ):
        try:
            download_url = import_storage.presign_get(
                export.file_key, download_name=export.filename,
            )
        except Exception:  # noqa: BLE001 - a presign hiccup must not break polling
            download_url = ""
    return {
        "id": str(export.export_id),
        "report_key": export.report_key,
        "status": export.status,
        "filename": export.filename,
        "row_count": export.row_count,
        "error": export.error_log,
        "created_at": export.created_at.isoformat() if export.created_at else None,
        "finished_at": export.finished_at.isoformat() if export.finished_at else None,
        "download_url": download_url,
    }


class StartReportExportView(PortalAPIView):
    """POST: start a background CSV export for ``report_key`` with ``params``.

    Returns the ReportExport job (poll GET /reports/exports/<id>/). When S3 isn't
    configured (local dev), there's no worker/storage, so it STREAMS the CSV back
    synchronously instead -- the frontend detects the CSV response and downloads
    it directly (no polling)."""

    def post(self, request):
        agent = current_agent(request)
        if not _is_management(agent):
            return Response({"detail": "Management access required."}, status=403)

        from ..services import import_storage
        from .report_exports import REPORT_BUILDERS, default_filename, stream_csv_response

        report_key = (request.data.get("report_key") or "").strip()
        params = request.data.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        builder = REPORT_BUILDERS.get(report_key)
        if builder is None:
            return Response({"error": f"Unknown report: {report_key!r}"}, status=400)

        filename = default_filename(report_key)
        if not import_storage.s3_enabled():
            # Dev / no-S3 fallback: stream the CSV directly (no background job).
            return stream_csv_response(builder(params), filename)

        export = ReportExport.objects.create(
            report_key=report_key, params=params, filename=filename,
            requested_by=agent,
        )
        from ..tasks import generate_report_export

        generate_report_export.delay(str(export.export_id))
        return Response(_report_export_payload(export), status=201)


class ReportExportDetailView(PortalAPIView):
    """GET: poll a background export's status + (when done) its download URL."""

    def get(self, request, export_id):
        agent = current_agent(request)
        if not _is_management(agent):
            return Response({"detail": "Management access required."}, status=403)
        export = ReportExport.objects.filter(pk=export_id).first()
        if export is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(_report_export_payload(export))

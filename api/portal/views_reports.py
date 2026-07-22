"""Admin > Reports: management-only CSV exports.

Each report is a management-gated endpoint that streams a downloadable CSV. The
first report exports members carrying a given lead source (e.g. the "Hyphen Met"
CallTools queue) within an optional Client.created_at date range.
"""

import csv
import functools
import operator
import re

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
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
    ProductTypeKind,
    ServiceAuthorizationStatus,
    SocialCareCoverageStatus,
    UniteUsAgent,
)
from ..services.catalog import product_type_kind_for_name
from .base import PortalAPIView, current_agent
from .serializers import (
    active_enrollment,
    active_member_profile,
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

        created_from = _parse_date(request.query_params.get("created_from"))
        created_to = _parse_date(request.query_params.get("created_to"))

        # Match ANY of the selected sources (case-insensitive).
        source_filter = functools.reduce(
            operator.or_, (Q(lead_source__iexact=v) for v in lead_sources)
        )
        qs = (
            Client.objects.filter(source_filter)
            .prefetch_related(
                "insurances",
                "screenings",
                "cases",
                "household_membership__household__members",
            )
            .order_by("created_at")
        )
        if created_from:
            qs = qs.filter(created_at__date__gte=created_from)
        if created_to:
            qs = qs.filter(created_at__date__lte=created_to)

        label_map = _lead_source_label_map()

        response = HttpResponse(content_type="text/csv")
        filename = f"members_by_lead_source_{timezone.localdate().isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Member ID",
            "Phone Number",
            "Medicaid ID",
            "Is there Screening",
            "Is there Eligibility",
            "Is there Internal Service Case",
            "Household Member Count (if multi-member)",
            "Lead Source",
        ])

        for client in qs:
            cases = list(client.cases.all())
            has_screening = len(list(client.screenings.all())) > 0
            has_eligibility = any(c.case_type == CaseType.ELIGIBILITY for c in cases)
            has_internal_service = any(
                c.case_type == CaseType.INTERNAL_SERVICE for c in cases
            )

            # Household member count only when the client belongs to a
            # multi-member household; blank otherwise (per the report spec).
            household_count = ""
            membership = getattr(client, "household_membership", None)
            if membership is not None:
                member_total = len(list(membership.household.members.all()))
                if member_total > 1:
                    household_count = member_total

            raw_source = (client.lead_source or "").strip()
            source_label = label_map.get(raw_source, raw_source)

            writer.writerow([
                str(client.client_id),
                client.client_phone_number or "",
                medicaid_member_id(client),
                "Yes" if has_screening else "No",
                "Yes" if has_eligibility else "No",
                "Yes" if has_internal_service else "No",
                household_count,
                source_label,
            ])

        return response


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
    """Management-only CSV export of members whose household enrollment is
    awaiting verification (stage ``pending_verification``), filtered by the
    enrollment's created date (``opened_at``).

    Columns: Client ID, First Name, Last Name, Phone Numbers.

    Query params:
        created_from -- optional inclusive lower bound on the enrollment's
                        created (opened_at) date.
        created_to   -- optional inclusive upper bound on the enrollment's
                        created (opened_at) date.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        created_from = _parse_date(request.query_params.get("created_from"))
        created_to = _parse_date(request.query_params.get("created_to"))

        qs = (
            EnrollmentVerification.objects.filter(
                stage=EnrollmentStage.PENDING_VERIFICATION
            )
            .select_related("client", "household")
            .prefetch_related(
                "member_profiles__client__phones",
                "household__members__client__phones",
            )
            .order_by("opened_at")
        )
        if created_from:
            qs = qs.filter(opened_at__date__gte=created_from)
        if created_to:
            qs = qs.filter(opened_at__date__lte=created_to)

        response = HttpResponse(content_type="text/csv")
        filename = (
            f"members_pending_verification_{timezone.localdate().isoformat()}.csv"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(["Client ID", "First Name", "Last Name", "Phone Numbers"])

        seen = set()
        for enr in qs:
            for client in _enrollment_member_clients(enr):
                if client is None or client.pk in seen:
                    continue
                seen.add(client.pk)
                writer.writerow([
                    str(client.client_id),
                    client.first_name or "",
                    client.last_name or "",
                    _client_phone_numbers(client),
                ])

        return response


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
    """ISO date string for a date/datetime, or "" when None."""
    if value is None:
        return ""
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


class AllMembersReportView(PortalAPIView):
    """Management-only CSV export of every member (Client), one row per member.

    Columns: Client ID, Client Name, Medicaid Plan, Medicaid Type, Insurance
    Effective Date, Insurance Expiration Date, Social Care Coverage Status, SCC
    Expiration Date, Enrollment Platform, Out of Orbit?, Out of Orbit Reason, Out
    of Range, Client Eligibility, Cadence, Facility, Menu Type, Dietary
    Restrictions, Currently Servicing.

    Query params (both optional; omit both to export every member):
        created_from -- inclusive lower bound on the member's created date.
        created_to   -- inclusive upper bound on the member's created date.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        created_from = _parse_date(request.query_params.get("created_from"))
        created_to = _parse_date(request.query_params.get("created_to"))

        qs = (
            Client.objects.all()
            .prefetch_related(
                "insurances",
                "social_care_coverages",
                "addresses",
                "member_profiles",
                "enrollments__kitchen",
                "enrollments__delivery_schedules",
                "household_membership__household__enrollment_verifications__kitchen",
                "household_membership__household__enrollment_verifications__delivery_schedules",
            )
            .order_by("last_name", "first_name", "created_at")
        )
        if created_from:
            qs = qs.filter(created_at__date__gte=created_from)
        if created_to:
            qs = qs.filter(created_at__date__lte=created_to)

        # Live eligibility inputs, resolved once for the whole run.
        from ..services.eligibility import evaluate_client
        from ..services.service_area import excluded_zips
        from ..services.state_area import allowed_state_codes

        zips = excluded_zips()
        states = allowed_state_codes()

        response = HttpResponse(content_type="text/csv")
        filename = f"all_members_{timezone.localdate().isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Client ID",
            "Client Name",
            "Medicaid Plan",
            "Medicaid Type",
            "Insurance Effective Date",
            "Insurance Expiration Date",
            "Social Care Coverage Status",
            "SCC Expiration Date",
            "Enrollment Platform",
            "Out of Orbit?",
            "Out of Orbit Reason",
            "Out of Range",
            "Client Eligibility",
            "Cadence",
            "Facility",
            "Menu Type",
            "Dietary Restrictions",
            "Currently Servicing",
        ])

        for client in qs:
            med = _medicaid_insurance(client)
            scc = _social_care_coverage(client)
            enr = active_enrollment(client)
            profile = active_member_profile(client)

            out_of_orbit = member_out_of_orbit(client)
            reason = _out_of_orbit_reason(enr, profile) if out_of_orbit else ""

            verdict = evaluate_client(client, zips=zips, states=states)
            eligibility = "Ineligible" if verdict.ineligible else "Eligible"

            facility = ""
            if enr is not None and enr.kitchen_id:
                facility = enr.kitchen.name or ""

            writer.writerow([
                str(client.client_id),
                f"{client.first_name or ''} {client.last_name or ''}".strip(),
                (med.plan_name if med else ""),
                _medicaid_type_label(med.plan_name if med else ""),
                _date_str(med.enrolled_at if med else None),
                _date_str(med.expired_at if med else None),
                (_SCC_STATUS_LABELS.get(scc.status, scc.status) if scc else ""),
                _date_str(scc.expired_at if scc else None),
                "UniteUs",
                _yn(out_of_orbit),
                reason,
                _yn(member_out_of_range(client)),
                eligibility,
                _cadence_label(profile, enr),
                facility,
                (profile.menu_type if profile else ""),
                _dietary_restrictions(profile),
                _currently_servicing(enr),
            ])

        return response


class CasesReportView(PortalAPIView):
    """Management-only CSV export of cases, one row per case, optionally limited
    to a case-created (``date_opened``) date range.

    Columns: Client ID, Case ID, Member Name, Member Phone Number, Team of Case
    Creator, Case Created By Name, Case Created Date, Case Closed Date, Case
    Status, Originating Provider Name, Provider Name, Program Name, Is Program
    Household?, Case Type, Meals/Boxes, Primary Worker Name, Care Coordinator,
    Service Authorization Status, Service Authorization End Date.

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

        created_from = _parse_date(request.query_params.get("created_from"))
        created_to = _parse_date(request.query_params.get("created_to"))

        qs = (
            Case.objects.select_related("client")
            .prefetch_related("client__phones")
            .order_by("date_opened")
        )
        if created_from:
            qs = qs.filter(date_opened__date__gte=created_from)
        if created_to:
            qs = qs.filter(date_opened__date__lte=created_to)

        # Case-creator team (Unite Us user_id -> Originating Team) and care
        # coordinator (agent_code -> agent name) lookups, built once.
        team_map = {
            str(u.user_id): (u.originating_team or "")
            for u in UniteUsAgent.objects.all()
        }
        coord_map = {
            a.agent_code: a.name
            for a in Agent.objects.exclude(agent_code__isnull=True).exclude(agent_code="")
        }
        auth_status_labels = dict(ServiceAuthorizationStatus.choices)

        response = HttpResponse(content_type="text/csv")
        filename = f"cases_{timezone.localdate().isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Client ID",
            "Case ID",
            "Member Name",
            "Member Phone Number",
            "Team of Case Creator",
            "Case Created By Name",
            "Case Created Date",
            "Case Closed Date",
            "Case Status",
            "Originating Provider Name",
            "Provider Name",
            "Program Name",
            "Is Program Household?",
            "Case Type",
            "Meals/Boxes",
            "Primary Worker Name",
            "Care Coordinator",
            "Service Authorization Status",
            "Service Authorization End Date",
        ])

        for case in qs:
            client = case.client
            # Team: on-roster Unite Us creators carry an Originating Team; a
            # creator not on the roster is Met Council staff. Blank when the case
            # has no recorded creator.
            if case.created_by_id:
                team = team_map.get(str(case.created_by_id), "Met Council Team")
            else:
                team = ""

            care_coordinator = coord_map.get(case.agent_code, case.agent_code or "")
            case_status = (
                "Closed" if case.case_status in _CLOSED_CASE_STATUSES else "Open"
            )
            auth_status = case.service_authorization_status_label or auth_status_labels.get(
                case.service_authorization_status, case.service_authorization_status or ""
            )

            writer.writerow([
                str(case.client_id),
                str(case.case_id),
                f"{client.first_name or ''} {client.last_name or ''}".strip() if client else "",
                _primary_phone(client) if client else "",
                team,
                case.created_by_name or "",
                _date_str(case.date_opened),
                _date_str(case.case_closed_at),
                case_status,
                case.originating_provider_name or "",
                case.provider_name or "",
                case.program_name or "",
                _yn("household" in (case.program_name or "").casefold()),
                case.get_case_type_display(),
                _meals_or_boxes(case.program_name),
                case.primary_worker_name or "",
                care_coordinator,
                auth_status,
                _date_str(case.service_authorization_approval_ends_at),
            ])

        return response

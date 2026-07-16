"""Admin > Reports: management-only CSV exports.

Each report is a management-gated endpoint that streams a downloadable CSV. The
first report exports members carrying a given lead source (e.g. the "Hyphen Met"
CallTools queue) within an optional Client.created_at date range.
"""

import csv
import functools
import operator

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    AddressType,
    Case,
    CaseStatus,
    CaseType,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
)
from .base import PortalAPIView, current_agent
from .serializers import internal_service_case, medicaid_member_id
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


def _active_medicaid_id(client):
    """Member id from an ACTIVE Medicaid plan (primary preferred). Falls back to
    :func:`medicaid_member_id` (any Medicaid / primary insurance) when no active
    Medicaid plan carries an id."""
    plans = list(client.insurances.all())
    active = [
        p for p in plans
        if p.plan_type == "medicaid" and p.status == "active" and p.external_member_id
    ]
    if active:
        primary = next((p for p in active if p.is_primary), active[0])
        return primary.external_member_id
    return medicaid_member_id(client)


def _pick_address(client):
    """The member's best own address: Current, then Home, then any. None when
    the member has no address on file."""
    addresses = list(client.addresses.all())
    if not addresses:
        return None
    for wanted in (AddressType.CURRENT, AddressType.HOME):
        for a in addresses:
            if a.type == wanted:
                return a
    return addresses[0]


def _household_primary(household):
    """The primary member client of a household (or None)."""
    if household is None:
        return None
    for hm in household.members.all():
        if hm.is_primary and hm.client_id:
            return hm.client
    return None


class AllMembersReportView(PortalAPIView):
    """Management-only CSV export of every member (Client), one row per member.

    Household-scoped columns (primary member id, total members) repeat the
    household's value across each of its members; a member with no household is
    treated as their own single-member household. Address falls back to the
    household primary's address when the member has none of their own (household
    members share a delivery address).

    Columns: Household Primary Member ID, Member ID, Medicaid ID (active), Name,
    Phone Number, Street Address, Apt, City, State, Zip, DOB, Internal Service
    Program Name, Is there Screening, Is there Eligibility, Is there Navigation,
    Is there Internal Service Case, Total members in household, Lead Source in
    CRM, Authorized amount for the open internal service case.
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        qs = (
            Client.objects.all()
            .prefetch_related(
                "insurances",
                "screenings",
                "cases",
                "addresses",
                "phones",
                "household_membership__household__members__client__addresses",
            )
            .order_by(
                "household_membership__household__household_id",
                "-household_membership__is_primary",
                "last_name",
                "first_name",
                "created_at",
            )
        )

        label_map = _lead_source_label_map()

        response = HttpResponse(content_type="text/csv")
        filename = f"all_members_{timezone.localdate().isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Household Primary Member ID",
            "Member ID",
            "Medicaid ID (active)",
            "Name",
            "Phone Number",
            "Street Address",
            "Apt",
            "City",
            "State",
            "Zip",
            "DOB",
            "Internal Service Program Name",
            "Is there Screening",
            "Is there Eligibility",
            "Is there Navigation",
            "Is there Internal Service Case",
            "Total members in household",
            "Lead Source in CRM",
            "Authorized amount for the open internal service case",
        ])

        for client in qs:
            cases = list(client.cases.all())
            has_screening = len(list(client.screenings.all())) > 0
            has_eligibility = any(c.case_type == CaseType.ELIGIBILITY for c in cases)
            has_navigation = any(c.case_type == CaseType.NAVIGATION for c in cases)
            has_internal_service = any(
                c.case_type == CaseType.INTERNAL_SERVICE for c in cases
            )

            # Household context: primary id repeats across a household's members;
            # a member with no household is their own single-member household.
            membership = getattr(client, "household_membership", None)
            household = membership.household if membership is not None else None
            primary_client = _household_primary(household) or client
            member_total = (
                len(list(household.members.all())) if household is not None else 1
            )

            # Internal-service program name (governing case, any status) + the
            # authorized amount from the OPEN internal-service case.
            gov_internal = internal_service_case(client)
            program_name = ""
            if gov_internal is not None:
                program_name = gov_internal.program_name or (
                    gov_internal.program.name if gov_internal.program_id else ""
                )
            open_internal = None
            if gov_internal is not None and gov_internal.case_status not in _CLOSED_CASE_STATUSES:
                open_internal = gov_internal
            else:
                opens = [
                    c for c in cases
                    if c.case_type == CaseType.INTERNAL_SERVICE
                    and c.case_status not in _CLOSED_CASE_STATUSES
                ]
                if opens:
                    open_internal = max(
                        opens,
                        key=lambda c: c.date_opened.timestamp() if c.date_opened else 0,
                    )
            authorized_amount = open_internal.authorized_amount if open_internal else ""

            # Address: the member's own (Current/Home/any), else the household
            # primary's (household members share a delivery address).
            addr = _pick_address(client)
            if addr is None and primary_client is not client:
                addr = _pick_address(primary_client)

            raw_source = (client.lead_source or "").strip()
            source_label = label_map.get(raw_source, raw_source)

            writer.writerow([
                str(primary_client.client_id),
                str(client.client_id),
                _active_medicaid_id(client),
                f"{client.first_name or ''} {client.last_name or ''}".strip(),
                _primary_phone(client),
                (addr.street if addr else ""),
                (addr.unit if addr else ""),
                (addr.city if addr else ""),
                (addr.state if addr else ""),
                (addr.zip if addr else ""),
                client.date_of_birth.isoformat() if client.date_of_birth else "",
                program_name,
                "Yes" if has_screening else "No",
                "Yes" if has_eligibility else "No",
                "Yes" if has_navigation else "No",
                "Yes" if has_internal_service else "No",
                member_total,
                source_label,
                authorized_amount,
            ])

        return response

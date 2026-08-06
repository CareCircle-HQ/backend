"""Request-independent CSV row generators for Admin > Reports.

Each ``*_rows(params)`` generator yields the header list first, then one list per
data row, driven only by a plain ``params`` dict (no request/response). This is
the SINGLE source of truth for a report's columns/rows, shared by:
  * the synchronous streaming endpoints (no-S3 fallback / small pulls), and
  * the background export Celery task (``generate_report_export``).

Registered in ``REPORT_BUILDERS`` keyed by ``report_key`` (the same keys the
frontend ``useReportExport`` hook and the ``ReportExport`` rows use).

Generators use ``queryset.iterator(chunk_size=...)`` to bound memory on the
large reports (All Members spans tens of thousands of rows).
"""
import csv

from django.http import StreamingHttpResponse
from django.utils import timezone


# --- streaming helper -------------------------------------------------------
class _Echo:
    """A file-like whose write() returns the value (for csv.writer streaming)."""

    def write(self, value):
        return value


def stream_csv_response(rows, filename):
    """A StreamingHttpResponse that writes ``rows`` (iterable of lists) as CSV.

    Starts sending immediately + keeps memory bounded, so even the largest
    report doesn't buffer the whole file or stall the proxy before the first
    byte. Used by the sync report endpoints (the background task writes the same
    rows to a temp file instead)."""
    writer = csv.writer(_Echo())
    resp = StreamingHttpResponse(
        (writer.writerow(r) for r in rows), content_type="text/csv",
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def default_filename(report_key):
    """Download filename for a report_key, e.g. all_members_2026-08-03.csv."""
    today = timezone.localdate().isoformat()
    slug = report_key.replace("-", "_")
    return f"{slug}_{today}.csv"


# --- All Members ------------------------------------------------------------
_ALL_MEMBERS_HEADER = [
    # Identification
    "Household Primary Member ID",
    "Member ID",
    "Medicaid ID (active)",
    "Member Name",
    "DOB",
    # Contact & Address
    "Phone Number",
    "Street Address",
    "Apt",
    "City",
    "State",
    "Zip",
    # Household
    "Total members in household",
    # Program & Case Status
    "Internal Service Program Name",
    "Team of Case Creator",
    "Is there Screening",
    "Is there Eligibility",
    "Is there Navigation",
    "Is there Internal Service Case",
    "Client Eligibility",
    "Eligible for:",
    "Currently servicing",
    "Cadence",
    "Facility",
    "Out of Orbit?",
    "Out of Orbit reason",
    "Out of Range",
    "Authorized amount for the open internal service case",
    # Lead & Enrollment
    "Lead Source in CRM",
    "Enrollment Platform",
    "Enrolled By",
    # Insurance / Medicaid
    "Medicaid Plan",
    "Medicaid Type",
    "Insurance Effective Date",
    "Insurance Expiration Date",
    # Social Care Coverage
    "Social Care Coverage Status",
    "Social Care Coverage Expiration Date",
    # Extras
    "Menu Type",
    "Dietary Restrictions",
]


def all_members_rows(params):
    """One row per member (Client). ``params``: created_from / created_to
    (inclusive, on Client.created_at)."""
    from ..models import CaseType, Client, UniteUsAgent
    from ..services.eligibility import evaluate_client
    from ..services.service_area import excluded_zips
    from ..services.state_area import allowed_state_codes
    from .serializers import (
        _assessment_eligible, active_enrollment, active_member_profile,
        internal_service_case, medicaid_member_id, member_out_of_orbit,
        member_out_of_range,
    )
    from .views_members import _parse_date
    from .views_reports import (
        _cadence_label, _client_phone_numbers, _currently_servicing,
        _current_address, _date_str, _dietary_restrictions,
        _household_member_count, _household_primary_member_id,
        _lead_source_label_map, _medicaid_insurance, _medicaid_type_label,
        _out_of_orbit_reason, _social_care_coverage, _SCC_STATUS_LABELS, _yn,
    )

    created_from = _parse_date(params.get("created_from"))
    created_to = _parse_date(params.get("created_to"))

    qs = (
        Client.objects.all()
        .prefetch_related(
            "insurances", "social_care_coverages", "addresses", "phones", "cases",
            "screenings", "assessments", "member_profiles",
            "enrollments__kitchen", "enrollments__delivery_schedules",
            "enrollments__verified_by",
            "household_membership__household__members",
            "household_membership__household__enrollment_verifications__kitchen",
            "household_membership__household__enrollment_verifications__delivery_schedules",
            "household_membership__household__enrollment_verifications__verified_by",
        )
        .order_by("last_name", "first_name", "created_at")
    )
    if created_from:
        qs = qs.filter(created_at__date__gte=created_from)
    if created_to:
        qs = qs.filter(created_at__date__lte=created_to)

    zips = excluded_zips()
    states = allowed_state_codes()
    lead_labels = _lead_source_label_map()
    team_map = {
        str(u.user_id): (u.originating_team or "")
        for u in UniteUsAgent.objects.all()
    }

    yield list(_ALL_MEMBERS_HEADER)

    for client in qs.iterator(chunk_size=2000):
        med = _medicaid_insurance(client)
        scc = _social_care_coverage(client)
        enr = active_enrollment(client)
        profile = active_member_profile(client)
        addr = _current_address(client)
        cases = list(client.cases.all())
        isc = internal_service_case(client)
        if isc is not None and isc.created_by_id:
            isc_team = team_map.get(str(isc.created_by_id), "Met Council Team")
        else:
            isc_team = ""

        out_of_orbit = member_out_of_orbit(client)
        reason = _out_of_orbit_reason(enr, profile) if out_of_orbit else ""
        verdict = evaluate_client(client, zips=zips, states=states)
        eligibility = "Ineligible" if verdict.ineligible else "Eligible"

        facility = ""
        if enr is not None and enr.kitchen_id:
            facility = enr.kitchen.name or ""
        raw_source = (client.lead_source or "").strip()

        yield [
            _household_primary_member_id(client),
            str(client.client_id),
            medicaid_member_id(client),
            f"{client.first_name or ''} {client.last_name or ''}".strip(),
            _date_str(client.date_of_birth),
            _client_phone_numbers(client),
            (addr.street if addr else ""),
            (addr.unit if addr else ""),
            (addr.city if addr else ""),
            (addr.state if addr else ""),
            (addr.zip if addr else ""),
            _household_member_count(client),
            (isc.program_name if isc else ""),
            isc_team,
            _yn(len(client.screenings.all()) > 0),
            _yn(any(c.case_type == CaseType.ELIGIBILITY for c in cases)),
            _yn(any(c.case_type == CaseType.NAVIGATION for c in cases)),
            _yn(isc is not None),
            eligibility,
            "; ".join(_assessment_eligible(client)),
            _currently_servicing(enr),
            _cadence_label(profile, enr),
            facility,
            _yn(out_of_orbit),
            reason,
            _yn(member_out_of_range(client)),
            (isc.authorized_amount if isc else ""),
            lead_labels.get(raw_source, raw_source),
            "UniteUs",
            (enr.verified_by.name if (enr is not None and enr.verified_by_id) else ""),
            (med.plan_name if med else ""),
            _medicaid_type_label(med.plan_name if med else ""),
            _date_str(med.enrolled_at if med else None),
            _date_str(med.expired_at if med else None),
            (_SCC_STATUS_LABELS.get(scc.status, scc.status) if scc else ""),
            _date_str(scc.expired_at if scc else None),
            (profile.menu_type if profile else ""),
            _dietary_restrictions(profile),
        ]


# --- Members Pending Verification -------------------------------------------
def members_pending_verification_rows(params):
    """One row per household member on a pending-verification enrollment.
    ``params``: created_from / created_to (inclusive, on enrollment opened_at)."""
    from ..models import EnrollmentStage, EnrollmentVerification
    from .views_members import _parse_date
    from .views_reports import _client_phone_numbers, _enrollment_member_clients

    created_from = _parse_date(params.get("created_from"))
    created_to = _parse_date(params.get("created_to"))
    qs = (
        EnrollmentVerification.objects
        .filter(stage=EnrollmentStage.PENDING_VERIFICATION)
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

    yield ["Client ID", "First Name", "Last Name", "Phone Numbers"]
    seen = set()
    for enr in qs.iterator(chunk_size=1000):
        for client in _enrollment_member_clients(enr):
            if client is None or client.pk in seen:
                continue
            seen.add(client.pk)
            yield [
                str(client.client_id),
                client.first_name or "",
                client.last_name or "",
                _client_phone_numbers(client),
            ]


# --- Pending Verification over 1 month --------------------------------------
def pending_verification_over_month_rows(params):
    """One row per household member on an enrollment that has been PENDING
    VERIFICATION for more than one month (opened over 30 days ago and still not
    verified). Columns: First Name, Last Name, Phone, Client ID, Case ID."""
    from datetime import timedelta

    from ..models import EnrollmentStage, EnrollmentVerification
    from ..services.lifecycle import governing_internal_case
    from .views_reports import _client_phone_numbers, _enrollment_member_clients

    cutoff = timezone.now() - timedelta(days=30)
    qs = (
        EnrollmentVerification.objects
        .filter(stage=EnrollmentStage.PENDING_VERIFICATION, opened_at__lt=cutoff)
        .select_related("client", "household", "case")
        .prefetch_related(
            "member_profiles__client__phones",
            "household__members__client__phones",
        )
        .order_by("opened_at")
    )

    yield ["First Name", "Last Name", "Phone", "Client ID", "Case ID"]
    seen = set()
    for enr in qs.iterator(chunk_size=1000):
        # The enrollment's own case, else the household's governing internal case.
        case_id = str(enr.case_id) if enr.case_id else ""
        if not case_id:
            gc = governing_internal_case(enr)
            case_id = str(gc.case_id) if gc is not None else ""
        for client in _enrollment_member_clients(enr):
            if client is None or client.pk in seen:
                continue
            seen.add(client.pk)
            yield [
                client.first_name or "",
                client.last_name or "",
                _client_phone_numbers(client),
                str(client.client_id),
                case_id,
            ]


# --- All Verifications ------------------------------------------------------
def all_verifications_rows(params):
    """COMPLETED verifications only -- one row per household/individual, so the
    count matches the Verification page filtered to "Verified" + a completed-date
    range. Authorization status is irrelevant here (a verification counts once its
    pop-up completed, regardless of the case's auth outcome).

    ``params``: completed_from / completed_to (inclusive, on ``verified_at`` --
    the Verification page's "verification completed" date, i.e. Verification page
    ``completed_from``/``completed_to``)."""
    from ..models import (
        EnrollmentStage, EnrollmentVerification, ServiceAuthorizationStatus,
    )
    from ..services.lifecycle import governing_internal_case
    from .views_members import _parse_date
    from .views_reports import _date_str

    completed_from = _parse_date(params.get("completed_from"))
    completed_to = _parse_date(params.get("completed_to"))
    qs = (
        EnrollmentVerification.objects
        # ONLY completed verifications; dismissed (Disregarded) requests never count.
        .filter(verified_at__isnull=False)
        .exclude(stage=EnrollmentStage.DISREGARDED)
        .select_related("client", "household", "case")
        .prefetch_related("client__cases")
        .order_by("-verified_at", "-opened_at")
    )
    if completed_from:
        qs = qs.filter(verified_at__date__gte=completed_from)
    if completed_to:
        qs = qs.filter(verified_at__date__lte=completed_to)

    yield [
        "Member ID", "Household/Individual", "Verification Requested",
        "Verification Completed", "Authorization Approved",
    ]
    # One row per HOUSEHOLD (else per client for solo members with no household
    # record), keeping the most-recent verified enrollment -- mirrors how the
    # Verification page groups members into household/individual rows, so the row
    # count matches the page's Verified count exactly.
    seen = set()
    for enr in qs.iterator(chunk_size=1000):
        key = ("hh", enr.household_id) if enr.household_id else ("c", enr.client_id)
        if key in seen:
            continue
        seen.add(key)
        client = enr.client
        scope = "Household" if "household" in (enr.program_name or "").casefold() else "Individual"
        gov = governing_internal_case(enr) or enr.case
        auth_approved = None
        if gov is not None and gov.service_authorization_status in (
            ServiceAuthorizationStatus.APPROVED,
            ServiceAuthorizationStatus.NOT_REQUIRED,
        ):
            auth_approved = gov.service_authorization_approval_starts_at
        yield [
            str(client.client_id) if client else "",
            scope,
            _date_str(enr.requested_at),
            _date_str(enr.verified_at),
            _date_str(auth_approved),
        ]


# --- Unite Us Agents --------------------------------------------------------
def unite_us_agents_rows(params):
    from ..models import UniteUsAgent

    yield ["Status", "User ID", "Name", "Email", "Team"]
    for a in UniteUsAgent.objects.all().iterator(chunk_size=2000):
        full_name = a.name or " ".join(p for p in [a.first_name, a.last_name] if p)
        yield [
            (a.status or "").title(), str(a.user_id), full_name,
            a.email or "", a.originating_team or "",
        ]


# --- Members by Lead Source -------------------------------------------------
def members_by_lead_source_rows(params):
    """One row per member carrying any of ``params['lead_sources']`` (list).
    Also honors created_from / created_to."""
    import functools
    import operator

    from django.db.models import Q

    from ..models import CaseType, Client
    from .serializers import medicaid_member_id
    from .views_members import _parse_date
    from .views_reports import _lead_source_label_map

    yield [
        "Member ID", "Phone Number", "Medicaid ID", "Is there Screening",
        "Is there Eligibility", "Is there Internal Service Case",
        "Household Member Count (if multi-member)", "Lead Source",
    ]
    lead_sources = [s for s in (params.get("lead_sources") or []) if s]
    if not lead_sources:
        return
    created_from = _parse_date(params.get("created_from"))
    created_to = _parse_date(params.get("created_to"))
    source_filter = functools.reduce(
        operator.or_, (Q(lead_source__iexact=v) for v in lead_sources)
    )
    qs = (
        Client.objects.filter(source_filter)
        .prefetch_related(
            "insurances", "screenings", "cases",
            "household_membership__household__members",
        )
        .order_by("created_at")
    )
    if created_from:
        qs = qs.filter(created_at__date__gte=created_from)
    if created_to:
        qs = qs.filter(created_at__date__lte=created_to)
    label_map = _lead_source_label_map()

    for client in qs.iterator(chunk_size=2000):
        cases = list(client.cases.all())
        household_count = ""
        membership = getattr(client, "household_membership", None)
        if membership is not None:
            member_total = len(list(membership.household.members.all()))
            if member_total > 1:
                household_count = member_total
        raw_source = (client.lead_source or "").strip()
        yield [
            str(client.client_id),
            client.client_phone_number or "",
            medicaid_member_id(client),
            "Yes" if len(list(client.screenings.all())) > 0 else "No",
            "Yes" if any(c.case_type == CaseType.ELIGIBILITY for c in cases) else "No",
            "Yes" if any(c.case_type == CaseType.INTERNAL_SERVICE for c in cases) else "No",
            household_count,
            label_map.get(raw_source, raw_source),
        ]


# --- Cases ------------------------------------------------------------------
def cases_rows(params):
    """One row per case. Honors created_from / created_to (on date_opened)."""
    from ..models import (
        Agent, Case, ServiceAuthorizationStatus, UniteUsAgent,
    )
    from .views_members import _parse_date
    from .views_reports import (
        _case_enrollment, _CLOSED_CASE_STATUSES, _date_str, _enrollment_cadence,
        _enrollment_kitchen, _meals_or_boxes, _primary_phone, _yn,
    )

    created_from = _parse_date(params.get("created_from"))
    created_to = _parse_date(params.get("created_to"))
    qs = (
        Case.objects.select_related("client")
        .prefetch_related(
            "client__phones", "enrollments__kitchen", "enrollments__delivery_schedules",
        )
        .order_by("date_opened")
    )
    if created_from:
        qs = qs.filter(date_opened__date__gte=created_from)
    if created_to:
        qs = qs.filter(date_opened__date__lte=created_to)

    team_map = {
        str(u.user_id): (u.originating_team or "")
        for u in UniteUsAgent.objects.all()
    }
    coord_map = {
        a.agent_code: a.name
        for a in Agent.objects.exclude(agent_code__isnull=True).exclude(agent_code="")
    }
    auth_status_labels = dict(ServiceAuthorizationStatus.choices)

    yield [
        "Client ID", "Case ID", "Member Name", "Member Phone Number",
        "Team of Case Creator", "Case Created By Name", "Case Created Date",
        "Case Closed Date", "Case Status", "Originating Provider Name",
        "Provider Name", "Program Name", "Is Program Household?", "Case Type",
        "Meals/Boxes", "Kitchen", "Cadence", "Primary Worker Name",
        "Care Coordinator", "Service Authorization Status",
        "Service Authorization End Date",
    ]
    for case in qs.iterator(chunk_size=2000):
        client = case.client
        enr = _case_enrollment(case)
        if case.created_by_id:
            team = team_map.get(str(case.created_by_id), "Met Council Team")
        else:
            team = ""
        care_coordinator = coord_map.get(case.agent_code, case.agent_code or "")
        case_status = "Closed" if case.case_status in _CLOSED_CASE_STATUSES else "Open"
        auth_status = case.service_authorization_status_label or auth_status_labels.get(
            case.service_authorization_status, case.service_authorization_status or ""
        )
        yield [
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
            _enrollment_kitchen(enr),
            _enrollment_cadence(enr),
            case.primary_worker_name or "",
            care_coordinator,
            auth_status,
            _date_str(case.service_authorization_approval_ends_at),
        ]


# --- Active Members for PO --------------------------------------------------
def members_for_po_rows(params):
    """One row per member/delivery on the calendar. ``params``: scope ("date" |
    "week"), date (YYYY-MM-DD, future, for scope=date), cadence (optional)."""
    from datetime import timedelta

    from ..models import (
        HouseholdMember, OrderSchedule, OrderStatus,
        SERVICE_EXCLUDED_ENROLLMENT_STAGES, SERVICE_EXCLUDED_MEMBER_STATUSES,
    )
    from ..services.purchase_orders import (
        _WEEKDAY_CADENCE, _household_group_code, _household_is_primary,
        authorized_internal_service_case_exists, open_internal_service_case_exists,
    )
    from .serializers import internal_service_case
    from .views_members import _parse_date
    from .views_reports import _CADENCE_LABELS, _date_str, _meals_or_boxes, _yn

    yield [
        "Delivery Date", "HouseholdGroup", "PrimaryMemberID", "Client ID",
        "PrimaryHousehold", "Quantity", "Delivery Address", "Menu Type",
        "Meal Type", "Kitchen", "Cadence", "Case ID", "Case Status",
        "Case Authorization", "Member Status",
    ]
    today = timezone.localdate()
    scope = (params.get("scope") or "date").lower()
    cadence = ""
    if scope == "week":
        monday = today - timedelta(days=today.weekday())
        base = OrderSchedule.objects.filter(
            anticipated_delivery_date__gte=monday,
            anticipated_delivery_date__lte=monday + timedelta(days=6),
        )
    else:
        d = _parse_date(params.get("date"))
        if d is None:
            return
        base = OrderSchedule.objects.filter(anticipated_delivery_date=d)
        cadence = (params.get("cadence") or "").strip()

    qs = (
        base.filter(status=OrderStatus.SCHEDULED, member__isnull=False)
        .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        .exclude(member__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .annotate(_has_open_isc=open_internal_service_case_exists())
        .filter(_has_open_isc=True)
        .annotate(_has_auth_isc=authorized_internal_service_case_exists())
        .filter(_has_auth_isc=True)
        .select_related("member__client", "household", "kitchen")
        .prefetch_related("member__client__cases")
        .order_by(
            "anticipated_delivery_date", "household__name",
            "household_group_code", "member_name",
        )
    )
    rows = list(qs)
    if cadence:
        rows = [
            o for o in rows
            if o.anticipated_delivery_date
            and _WEEKDAY_CADENCE.get(o.anticipated_delivery_date.weekday()) == cadence
        ]
    hh_ids = {o.household_id for o in rows if o.household_id}
    primary_by_hh = {}
    if hh_ids:
        primary_by_hh = dict(
            HouseholdMember.objects.filter(
                household_id__in=hh_ids, is_primary=True
            ).values_list("household_id", "client_id")
        )

    for o in rows:
        client = o.member.client if (o.member and o.member.client_id) else None
        client_id = str(client.client_id) if client else ""
        primary_id = client_id
        if o.household_id and primary_by_hh.get(o.household_id):
            primary_id = str(primary_by_hh[o.household_id])
        weekday = o.anticipated_delivery_date.weekday() if o.anticipated_delivery_date else None
        meal_type = _meals_or_boxes(o.program_name)
        if not meal_type:
            meal_type = "Boxes" if weekday == 2 else "Meals"
        cad_code = _WEEKDAY_CADENCE.get(weekday, "") if weekday is not None else ""
        case = internal_service_case(client) if client else None
        case_id = str(case.case_id) if case else ""
        case_status = case.get_case_status_display() if case else ""
        case_authorization = ""
        if case:
            case_authorization = case.service_authorization_status_label or (
                case.get_service_authorization_status_display()
                if case.service_authorization_status else ""
            )
        member_status = o.member.get_status_display() if o.member else ""
        yield [
            _date_str(o.anticipated_delivery_date),
            _household_group_code(o.household),
            primary_id,
            client_id,
            _yn(_household_is_primary(client)),
            o.how_many_meals_or_boxes if o.how_many_meals_or_boxes is not None else 0,
            (o.delivery_address or "").replace("\n", ", ").strip(),
            o.menu_type or "",
            meal_type,
            (o.kitchen.name if o.kitchen_id else ""),
            _CADENCE_LABELS.get(cad_code, cad_code),
            case_id,
            case_status,
            case_authorization,
            member_status,
        ]


# --- Members Not on a PO ----------------------------------------------------
def members_not_served_rows(params):
    """One row per member who has an internal-service case but no live delivery."""
    from ..models import (
        CaseType, Client, EnrollmentStage, MemberStatus, OrderSchedule,
        OrderStatus, SERVICE_EXCLUDED_ENROLLMENT_STAGES,
        SERVICE_EXCLUDED_MEMBER_STATUSES,
    )
    from ..services.purchase_orders import (
        _household_group_code, _household_is_primary,
        authorized_internal_service_case_exists, open_internal_service_case_exists,
    )
    from .serializers import (
        active_member_profile, internal_service_case, member_out_of_orbit,
        member_out_of_range,
    )
    from .views_reports import (
        _cadence_label, _ENROLLMENT_STAGE_LABELS, _meals_or_boxes, _yn,
    )

    served_ids = set(
        OrderSchedule.objects.filter(
            status=OrderStatus.SCHEDULED, member__client__isnull=False
        )
        .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        .exclude(member__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .annotate(_has_open_isc=open_internal_service_case_exists())
        .filter(_has_open_isc=True)
        .annotate(_has_auth_isc=authorized_internal_service_case_exists())
        .filter(_has_auth_isc=True)
        .values_list("member__client_id", flat=True)
    )
    qs = (
        Client.objects.filter(cases__case_type=CaseType.INTERNAL_SERVICE)
        .exclude(client_id__in=served_ids)
        .distinct()
        .prefetch_related(
            "cases", "enrollments__member_profiles", "enrollments__kitchen",
            "enrollments__delivery_schedules", "member_profiles",
            "household_membership__household__members",
        )
        .order_by("last_name", "first_name")
    )

    yield [
        "Client ID", "Case ID", "Case Status", "Case Type", "Case Authorization",
        "Program Name", "Meals/Boxes", "Is Part of a Household", "Household Group",
        "Primary Member ID", "Is Primary", "Full Name", "Member Stage", "Kitchen",
        "Cadence", "Menu Type", "Out of Orbit", "Out of Range", "On Hold", "Cancelled",
    ]
    for client in qs.iterator(chunk_size=1000):
        case = internal_service_case(client)
        profile = active_member_profile(client)
        status = profile.status if profile else ""
        case_authorization = ""
        program_name = ""
        if case:
            case_authorization = case.service_authorization_status_label or (
                case.get_service_authorization_status_display()
                if case.service_authorization_status else ""
            )
            program_name = case.program_name or ""
        membership = getattr(client, "household_membership", None)
        household = membership.household if membership is not None else None
        members = list(household.members.all()) if household is not None else []
        in_household = len(members) > 1
        group_code = _household_group_code(household)
        primary_id = str(client.client_id)
        prim = next((m for m in members if m.is_primary), None)
        if prim is not None:
            primary_id = str(prim.client_id)
        enrollments = list(client.enrollments.all())
        latest = (
            max(enrollments, key=lambda e: e.opened_at or timezone.now())
            if enrollments else None
        )
        on_hold = (
            status == MemberStatus.PAUSED
            or (latest is not None and latest.stage == EnrollmentStage.ON_HOLD)
        )
        cancelled = (
            status == MemberStatus.INACTIVE
            or (latest is not None and latest.stage == EnrollmentStage.CANCELLED)
        )
        member_stage = (
            _ENROLLMENT_STAGE_LABELS.get(latest.stage, latest.stage or "")
            if latest is not None else ""
        )
        kitchen = latest.kitchen.name if (latest and latest.kitchen_id) else ""
        yield [
            str(client.client_id),
            str(case.case_id) if case else "",
            case.get_case_status_display() if case else "",
            case.get_case_type_display() if case else "",
            case_authorization,
            program_name,
            _meals_or_boxes(program_name),
            _yn(in_household),
            group_code,
            primary_id,
            _yn(_household_is_primary(client)),
            f"{client.first_name or ''} {client.last_name or ''}".strip(),
            member_stage,
            kitchen,
            _cadence_label(profile, latest),
            profile.menu_type if profile else "",
            _yn(member_out_of_orbit(client)),
            _yn(member_out_of_range(client)),
            _yn(on_hold),
            _yn(cancelled),
        ]


# ---------------------------------------------------------------------------
# Registry: report_key -> row generator.
# ---------------------------------------------------------------------------
REPORT_BUILDERS = {
    "members-by-lead-source": members_by_lead_source_rows,
    "members-pending-verification": members_pending_verification_rows,
    "pending-verification-over-month": pending_verification_over_month_rows,
    "all-verifications": all_verifications_rows,
    "all-members": all_members_rows,
    "cases": cases_rows,
    "members-for-po": members_for_po_rows,
    "members-not-served": members_not_served_rows,
    "unite-us-agents": unite_us_agents_rows,
}

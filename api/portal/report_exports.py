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
            "household_membership__household__members",
            "household_membership__household__enrollment_verifications__kitchen",
            "household_membership__household__enrollment_verifications__delivery_schedules",
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
            (med.plan_name if med else ""),
            _medicaid_type_label(med.plan_name if med else ""),
            _date_str(med.enrolled_at if med else None),
            _date_str(med.expired_at if med else None),
            (_SCC_STATUS_LABELS.get(scc.status, scc.status) if scc else ""),
            _date_str(scc.expired_at if scc else None),
            (profile.menu_type if profile else ""),
            _dietary_restrictions(profile),
        ]


# ---------------------------------------------------------------------------
# Registry: report_key -> row generator. Extend as reports are ported.
# ---------------------------------------------------------------------------
REPORT_BUILDERS = {
    "all-members": all_members_rows,
}

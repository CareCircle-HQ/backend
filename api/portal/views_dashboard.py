"""Management dashboard analytics.

A single aggregate endpoint (management-only) reporting on the internal-service
(meal/box) program. The six headline cards are scoped by the selected date range
(to internal-service cases OPENED within that window): Total Open Cases, Total
Members, Cancel Rate, Total Enrolled, Total Receiving Meals, and Total Pending
Meals. The date window is either a named ``period`` preset or an explicit custom
``start``/``end`` range (see :func:`resolve_window`). Meals vs Boxes is also
scoped to cases opened in the range; the Section-2 serving breakdown remains an
all-time live snapshot. ``All Time`` applies no date filter to any metric. See
:class:`DashboardView` for exact definitions.
"""

from datetime import date, timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.response import Response

from ..models import (
    Case,
    CaseStatus,
    CaseType,
    Client,
    ClientStage,
    EnrollmentStage,
    HouseholdMember,
    Insurance,
    InsurancePlanType,
    MemberDietaryProfile,
    MemberStatus,
    ProductTypeKind,
    RecordStatus,
    ServiceAuthorizationStatus,
    SocialCareCoverage,
    SocialCareCoverageStatus,
    Ticket,
    TicketStatus,
    TicketTypeCode,
)
from ..services.catalog import product_type_kind_for_name
from .base import PortalAPIView, current_agent

# Case statuses that mean the case is no longer open/serviceable.
_TERMINAL_CASE_STATUSES = [CaseStatus.CLOSED, CaseStatus.CANCELLED]

# The drill-down reasons the serving-status list endpoint understands. The first
# group mirrors the "Not Being Served" cards; the second is the follow-up
# watchlist (which can overlap with actively-served members).
_SERVING_REASONS = frozenset({
    "needs_verification", "rejected_case", "out_of_range",
    "programs_on_hold", "members_paused_agent", "members_paused_eligibility",
    "pending_closure", "out_of_orbit",
    "insurance_expiring", "no_social_coverage",
})


def period_window(period):
    """Map a dashboard period code to an inclusive ``(start, end)`` date window
    on the LOCAL calendar, or ``(None, None)`` for "all" (no date filter).

    Weeks start Monday; current periods end today. Defaults to "all".
    ``last_month`` is the previous calendar month.
    """
    period = (period or "").strip().lower()
    today = timezone.localdate()
    if period == "today":
        return today, today
    if period == "week":  # This Week
        return today - timedelta(days=today.weekday()), today
    if period == "month":  # This Month
        return today.replace(day=1), today
    if period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == "year":  # This Year
        return today.replace(month=1, day=1), today
    return None, None  # all time (default)


def resolve_window(request):
    """Resolve the dashboard's ``(start, end)`` date window from the request.

    An explicit custom range (``start`` / ``end`` query params, ISO YYYY-MM-DD)
    wins over the named ``period`` preset. A one-sided custom range is closed
    with a sensible default (missing start -> 2000-01-01, missing end -> today)
    so a custom window is ALWAYS fully bounded -- this preserves the
    "``start is None`` => all time" invariant the metric helpers rely on.
    """
    start_raw = (request.query_params.get("start") or "").strip()
    end_raw = (request.query_params.get("end") or "").strip()
    if start_raw or end_raw:
        start = parse_date(start_raw) if start_raw else None
        end = parse_date(end_raw) if end_raw else None
        if start is None:
            start = date(2000, 1, 1)
        if end is None:
            end = timezone.localdate()
        if end < start:
            start, end = end, start
        return start, end
    return period_window((request.query_params.get("period") or "all").lower())


def _scope_by_opened(qs, start, end):
    """Restrict a Case queryset to rows OPENED within [start, end]. No-op when
    the window is open-ended (all time). Rows with a NULL ``date_opened`` are
    dropped only when a window is applied."""
    if start is None:
        return qs
    return qs.filter(date_opened__date__gte=start, date_opened__date__lte=end)


def governing_internal_case_ids():
    """The ``case_id`` of each client's GOVERNING internal-service case, using
    the system-wide governing rule (:func:`governing_case_key` over the client's
    internal-service cases -- an approved authorization beats a denial regardless
    of dates, then OPEN over closed, then most recent).

    A case with NO real authorization -- a BLANK status or ``never_requested`` --
    can NEVER be a governing case: it's excluded from the candidate pool. So a
    client whose only internal-service case is blank/never-requested contributes
    NO governing case (and drops off every dashboard case metric), rather than
    that unauthorized case being counted as their open case.

    Every dashboard case metric is restricted to these ids, so a superseded or
    parallel NON-governing case is never counted or considered anywhere: a
    client contributes exactly ONE (their governing) internal-service case.
    """
    from ..services.lifecycle import governing_case_key

    # Statuses that confer no authorization and must never govern.
    _NON_GOVERNING = {"", ServiceAuthorizationStatus.NEVER_REQUESTED}

    best = {}
    for c in (
        Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
        .only(
            "case_id", "client_id", "service_authorization_status",
            "case_status", "case_created_at", "date_opened", "updated_at",
        )
    ):
        if (c.service_authorization_status or "") in _NON_GOVERNING:
            continue  # blank / never_requested can never be a governing case
        cur = best.get(c.client_id)
        if cur is None or governing_case_key(c) > governing_case_key(cur):
            best[c.client_id] = c
    return {c.case_id for c in best.values()}


def _case_product_kind(program_product_type, program_name, service_type):
    """Resolve a case's Meals/Boxes kind: the linked Program's ProductType wins;
    otherwise a keyword heuristic across the program/service names. Returns a
    ProductTypeKind value or None when it can't be determined."""
    if program_product_type:
        try:
            return ProductTypeKind(program_product_type)
        except ValueError:
            pass
    for name in (program_name, service_type):
        kind = product_type_kind_for_name(name)
        if kind:
            return kind
    return None


def _scope_members(qs, start, end):
    """Restrict a MemberDietaryProfile queryset to members tied to an in-range
    internal-service case. All-time (start is None) is a live snapshot with no
    case/date filter, mirroring the Section-2 counts in DashboardView."""
    if start is None:
        return qs
    return qs.filter(
        enrollment__case__case_type=CaseType.INTERNAL_SERVICE,
        enrollment__case__date_opened__date__gte=start,
        enrollment__case__date_opened__date__lte=end,
    )


def serving_client_ids(reason, *, start, end, governing_ids=None):
    """Distinct client_ids for a serving-status / watchlist ``reason``, scoped to
    the selected date range. Single source of truth for both the dashboard counts
    and the drill-down list, so the two never disagree on WHO is included.

    ``governing_ids`` is the precomputed set of GOVERNING internal-service
    case_ids (:func:`governing_internal_case_ids`); the case-driven reasons are
    restricted to it so a NON-governing case is never considered. Computed lazily
    when not supplied.

    Returns a set of client_ids, or ``None`` for an unknown reason.
    """
    mdp = MemberDietaryProfile.objects

    def scope(qs):
        return _scope_members(qs, start, end)

    def gov():
        return governing_ids if governing_ids is not None else governing_internal_case_ids()

    _open_gov_cache = {}

    def open_gov_clients():
        """client_ids whose GOVERNING internal-service case is currently OPEN
        (non-terminal). Every serving/watchlist reason is intersected with this
        so a member whose governing case has CLOSED is never flagged -- we only
        surface actionable members whose governing case is still open."""
        if "v" not in _open_gov_cache:
            _open_gov_cache["v"] = set(
                Case.objects.filter(case_id__in=gov())
                .exclude(case_status__in=_TERMINAL_CASE_STATUSES)
                .values_list("client_id", flat=True)
            )
        return _open_gov_cache["v"]

    if reason == "needs_verification":
        # Mirror the Verification page's "Pending + Requested-date" filter EXACTLY
        # (same helpers) so this card can never drift from what agents see there:
        # members in the verification scope (their household primary holds an
        # internal-service case) whose verification is NOT yet completed
        # (verified_at null), windowed by WHEN VERIFICATION WAS REQUESTED
        # (enrollment.opened_at). Counted by HOUSEHOLD -- collapse each qualifying
        # member to its household primary (the actionable case holder), so the
        # count equals the Verification page's "Showing N households".
        from .views_members import (
            apply_enrollment_date_filter,
            require_internal_service_primary,
            verification_completed_q,
            verification_scope_q,
        )

        clients = require_internal_service_primary(
            Client.objects.filter(verification_scope_q())
        ).exclude(verification_completed_q())
        if start is not None:
            clients = apply_enrollment_date_filter(clients, "opened_at", start, end)
        member_ids = set(clients.values_list("client_id", flat=True))
        return set(_primary_map(member_ids).values())
    if reason == "rejected_case":
        # Mirror the Total Open Cases "Rejected" definition EXACTLY (same Case
        # query: internal-service, opened in range, non-terminal, auth DENIED)
        # so the two cards can't disagree. Sourced from the Case table -- NOT
        # MemberDietaryProfile -- because a denied case need not have an
        # enrollment/dietary profile yet; the mdp path missed those.
        # Restricted to the GOVERNING case: a client is only "rejected" when the
        # case that governs them is denied -- a superseded/parallel denied case
        # while their governing case is approved must NOT flag them.
        denied = _scope_by_opened(
            Case.objects.filter(
                case_type=CaseType.INTERNAL_SERVICE,
                case_id__in=gov(),
                service_authorization_status=ServiceAuthorizationStatus.DENIED,
            ),
            start, end,
        ).exclude(case_status__in=_TERMINAL_CASE_STATUSES)
        return set(denied.values_list("client_id", flat=True))
    if reason == "out_of_range":
        return set(scope(
            mdp.filter(status=MemberStatus.OUT_OF_RANGE)
        ).values_list("client_id", flat=True)) & open_gov_clients()
    if reason == "out_of_orbit":
        return set(scope(
            mdp.filter(status=MemberStatus.OUT_OF_ORBIT)
        ).values_list("client_id", flat=True)) & open_gov_clients()
    if reason == "programs_on_hold":
        # PROGRAM-level, matching the Members page "On Hold" filter EXACTLY: the
        # member's GOVERNING enrollment is On Hold (governing_enrollment_stage --
        # so a stray/superseded on-hold enrollment alongside a live one is
        # ignored), the member is ELIGIBLE (not on the Ineligible off-ramp), and
        # the GOVERNING internal-service case is OPEN. Counted per PROGRAM
        # (collapsed to the household primary).
        from .views_members import governing_enrollment_stage

        # "Open" here mirrors the Members page "Internal Service = Open" filter:
        # the client has ANY non-terminal internal-service case (Exists), NOT
        # specifically that the governing case is open.
        open_case_clients = set(
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
            .exclude(case_status__in=_TERMINAL_CASE_STATUSES)
            .values_list("client_id", flat=True)
        )
        cq = (
            Client.objects.annotate(_gov=governing_enrollment_stage())
            .filter(_gov=EnrollmentStage.ON_HOLD)
            .exclude(lifecycle_stage=ClientStage.INELIGIBLE)
        )
        if start is not None:  # mirror scope(): in-range internal-service case
            cq = cq.filter(
                cases__case_type=CaseType.INTERNAL_SERVICE,
                cases__date_opened__date__gte=start,
                cases__date_opened__date__lte=end,
            )
        member_ids = set(cq.values_list("client_id", flat=True)) & open_case_clients
        return set(_primary_map(member_ids).values())
    if reason in ("members_paused_agent", "members_paused_eligibility"):
        # PROGRAMS with a PAUSED member, split by WHO paused them: an agent
        # (manual, eligibility_paused=False) vs eligibility (auto,
        # eligibility_paused=True). Mirrors the Members page chain: any
        # eligibility ("All") -> Internal Service = Open (client has ANY
        # non-terminal internal-service case) -> Paused. No Eligible filter --
        # eligibility-paused members are on the Ineligible off-ramp, so requiring
        # Eligible would drop them all. Counted per PROGRAM (collapse the paused
        # members to their household primary so the link drives to the program).
        open_case_clients = set(
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
            .exclude(case_status__in=_TERMINAL_CASE_STATUSES)
            .values_list("client_id", flat=True)
        )
        paused = set(scope(mdp.filter(
            status=MemberStatus.PAUSED,
            eligibility_paused=(reason == "members_paused_eligibility"),
        )).values_list("client_id", flat=True)) & open_case_clients
        return set(_primary_map(paused).values())
    if reason == "pending_closure":
        # Only a closure ticket on the GOVERNING case flags the member -- a ticket
        # on a non-governing case is not their service closing.
        tickets = Ticket.objects.filter(
            type__code=TicketTypeCode.CASE_CLOSURE,
            case_id__in=gov(),
        ).exclude(status=TicketStatus.RESOLVED)
        if start is not None:
            tickets = tickets.filter(
                case__case_type=CaseType.INTERNAL_SERVICE,
                case__date_opened__date__gte=start,
                case__date_opened__date__lte=end,
            )
        return set(
            cid for cid in tickets.values_list("client_id", flat=True) if cid
        )

    # --- Watchlist: PROGRAMS with >=1 member lacking coverage ----------------
    # Program-level: a household is flagged when ANY of its members (primary or
    # dependent) lacks the coverage, and its GOVERNING internal-service case is
    # OPEN. "Enrolled" is therefore every member of a household whose primary
    # holds an open governing case; the lacking members are collapsed back to
    # their household primary so the count/drill-down is PROGRAMS, not members.
    open_primaries = open_gov_clients()
    member_ids = set(scope(mdp).values_list("client_id", flat=True))
    prim = _primary_map(member_ids)
    enrolled = {cid for cid in member_ids if prim.get(cid, cid) in open_primaries}
    if not enrolled:
        return set()

    def programs_lacking(have):
        return {prim.get(cid, cid) for cid in (enrolled - have)}

    if reason == "insurance_expiring":
        # "No active Medicaid": trust the imported STATUS (import doesn't reliably
        # carry an end date) -- a member with NO ACTIVE Medicaid/Dual plan lacks
        # it. Mirrors lifecycle.has_valid_medicaid.
        have = set(Insurance.objects.filter(
            client_id__in=enrolled,
            plan_type__in=[InsurancePlanType.MEDICAID, InsurancePlanType.DUAL],
            status=RecordStatus.ACTIVE,
        ).values_list("client_id", flat=True))
        return programs_lacking(have)
    if reason == "no_social_coverage":
        have = set(SocialCareCoverage.objects.filter(
            client_id__in=enrolled,
            status=SocialCareCoverageStatus.ENROLLED,
        ).values_list("client_id", flat=True))
        return programs_lacking(have)
    return None


def _primary_map(client_ids):
    """Map each client_id to its household's primary client_id (itself when the
    client has no household or no primary is flagged)."""
    memberships = list(
        HouseholdMember.objects.filter(client_id__in=client_ids)
        .values("client_id", "household_id")
    )
    household_ids = {m["household_id"] for m in memberships}
    primary_by_hh = {
        hm["household_id"]: hm["client_id"]
        for hm in HouseholdMember.objects.filter(
            household_id__in=household_ids, is_primary=True
        ).values("household_id", "client_id")
    }
    out = {}
    for m in memberships:
        out[m["client_id"]] = primary_by_hh.get(m["household_id"], m["client_id"])
    for cid in client_ids:
        out.setdefault(cid, cid)
    return out


def _fmt_date(dt):
    return dt.date().isoformat() if dt else None


def _serving_details(reason, client_ids, *, start, end, governing_ids=None):
    """Per-client presentation detail for a reason's list rows. Presentation only
    -- membership is decided by :func:`serving_client_ids`. ``governing_ids``
    mirrors that function's governing-case restriction."""
    if not client_ids:
        return {}
    ids = set(client_ids)
    mdp = MemberDietaryProfile.objects

    def gov():
        return governing_ids if governing_ids is not None else governing_internal_case_ids()

    if reason == "needs_verification":
        out = {}
        for p in (
            mdp.filter(client_id__in=ids)
            .select_related("enrollment__case")
            .order_by("client_id")
        ):
            if p.client_id in out:
                continue
            case = getattr(p.enrollment, "case", None)
            label = (getattr(case, "program_name", "") or "").strip()
            out[p.client_id] = label or "Internal service case"
        return out
    if reason == "rejected_case":
        # Source from the same Case query as the count (Case table, not mdp) so
        # denied cases without an enrollment/dietary profile still get a label.
        denied = _scope_by_opened(
            Case.objects.filter(
                case_type=CaseType.INTERNAL_SERVICE,
                case_id__in=gov(),
                service_authorization_status=ServiceAuthorizationStatus.DENIED,
            ),
            start, end,
        ).exclude(case_status__in=_TERMINAL_CASE_STATUSES).filter(client_id__in=ids)
        out = {}
        for row in denied.values("client_id", "program_name", "service_type"):
            if row["client_id"] in out:
                continue
            name = (row["program_name"] or row["service_type"] or "").strip()
            out[row["client_id"]] = (
                f"Denied · {name}" if name else "Case authorization denied"
            )
        return out
    if reason in ("out_of_range", "out_of_orbit"):
        target = (
            MemberStatus.OUT_OF_RANGE if reason == "out_of_range"
            else MemberStatus.OUT_OF_ORBIT
        )
        out = {}
        for p in mdp.filter(client_id__in=ids, status=target).order_by("client_id"):
            if p.client_id in out:
                continue
            since = _fmt_date(p.status_changed_at)
            out[p.client_id] = f"Since {since}" if since else ""
        return out
    if reason == "programs_on_hold":
        # ids are household primaries (the program holders). Label each with its
        # program name when available.
        out = {}
        for p in (
            mdp.filter(
                client_id__in=ids, enrollment__stage=EnrollmentStage.ON_HOLD
            )
            .select_related("enrollment__case")
            .order_by("client_id")
        ):
            if p.client_id in out:
                continue
            case = getattr(p.enrollment, "case", None)
            name = (getattr(case, "program_name", "") or "").strip()
            out[p.client_id] = f"Program on hold · {name}" if name else "Program on hold (under review)"
        for cid in ids:
            out.setdefault(cid, "Program on hold (under review)")
        return out
    if reason in ("members_paused_agent", "members_paused_eligibility"):
        # ids are program primaries. Count paused members (of the matching kind)
        # per program and label with the program name.
        elig = reason == "members_paused_eligibility"
        kind = "eligibility" if elig else "agent"
        paused_ids = list(mdp.filter(
            status=MemberStatus.PAUSED, eligibility_paused=elig,
        ).values_list("client_id", flat=True))
        prim = _primary_map(set(paused_ids))
        counts = {}
        for cid in paused_ids:
            pk = prim.get(cid, cid)
            counts[pk] = counts.get(pk, 0) + 1
        out = {}
        for p in (
            mdp.filter(client_id__in=ids)
            .select_related("enrollment__case")
            .order_by("client_id")
        ):
            if p.client_id in out:
                continue
            case = getattr(p.enrollment, "case", None)
            name = (getattr(case, "program_name", "") or "").strip()
            n = counts.get(p.client_id, 1)
            base = f"{n} member{'' if n == 1 else 's'} paused by {kind}"
            out[p.client_id] = f"{base} · {name}" if name else base
        return out
    if reason == "pending_closure":
        tickets = Ticket.objects.filter(
            client_id__in=ids, type__code=TicketTypeCode.CASE_CLOSURE,
        ).exclude(status=TicketStatus.RESOLVED).order_by("client_id")
        out = {}
        for t in tickets:
            if t.client_id in out:
                continue
            out[t.client_id] = (t.reason or "Case closure in progress")[:140]
        return out
    if reason in ("insurance_expiring", "no_social_coverage"):
        # ids are program primaries; label with the program name.
        prefix = (
            "Member(s) without active Medicaid"
            if reason == "insurance_expiring"
            else "Member(s) without social care coverage"
        )
        out = {}
        for p in (
            mdp.filter(client_id__in=ids)
            .select_related("enrollment__case")
            .order_by("client_id")
        ):
            if p.client_id in out:
                continue
            case = getattr(p.enrollment, "case", None)
            name = (getattr(case, "program_name", "") or "").strip()
            out[p.client_id] = f"{prefix} · {name}" if name else prefix
        for cid in ids:  # primaries without a profile row still get a label
            out.setdefault(cid, prefix)
        return out
    return {}


class DashboardView(PortalAPIView):
    """Management-only program dashboard.

    Three metrics are TIME-FRAME SENSITIVE -- scoped to Internal Service cases
    OPENED within the selected date range (``period``; ``all`` applies no date
    filter): open_cases, members, and cancel_rate. Every other metric is an
    ALL-TIME live snapshot. Metric definitions:

    Every case metric counts ONLY each client's GOVERNING internal-service case
    (:func:`governing_internal_case_ids`); a superseded / parallel non-governing
    case is never counted or considered anywhere.

    * open_cases  -- [TIME-FRAME] non-terminal (not Closed/Cancelled) GOVERNING
      internal-service cases (one per client), broken down by service-
      authorization outcome: Accepted (approved), Requested (pending), Rejected
      (denied).
    * members     -- [TIME-FRAME] distinct clients across those open cases'
      households, split into Primary members (household heads / case holders)
      vs Members of Household. A client with several open cases counts once.
    * cancel_rate -- [TIME-FRAME] attrition: distinct members who are Paused,
      have their household On Hold, are Out of Orbit / Out of Range, or hold a
      Cancelled enrollment, summed, as a percentage of the distinct members
      enrolled in accepted-authorization (open + APPROVED) internal-service
      cases (the base). Can exceed 100% since the lost buckets are not a strict
      subset of the accepted-case base.
    * total_enrolled -- [ALL TIME scalar] distinct members in the households of
      ALL internal-service cases (legacy field; the range-scoped Total Enrolled
      card reads the ``enrolled`` breakdown below).
    * enrolled / receiving / pending -- [TIME-FRAME] the three second-row cards
      (Total Enrolled, Total Receiving Meals, Total Pending Meals), scoped to
      members tied to an internal-service case opened in the selected range.
    * active_delivery_members -- [ALL TIME] distinct members currently ACTIVE in
      a Service-Active enrollment (Accepted + Verified + being served).
    * meals_boxes -- [TIME-FRAME] currently-open cases opened in the selected
      range, split by product kind (Meals/Boxes) and, within each, Individual
      vs Household.
    * serving -- Section-2 member serving-status breakdown. The Not Being Served
      + Needs Follow-up (watchlist) counts are [TIME-FRAME] (scoped to the range
      via serving_client_ids); receiving_meals + pending_meals are [ALL TIME].
    """

    def get(self, request):
        # --- Management-only gate -----------------------------------------
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        period = (request.query_params.get("period") or "all").lower()
        custom = bool(
            (request.query_params.get("start") or "").strip()
            or (request.query_params.get("end") or "").strip()
        )
        start, end = resolve_window(request)

        # Every case metric below is restricted to each client's GOVERNING
        # internal-service case, so a superseded / parallel NON-governing case is
        # never counted or considered anywhere on the dashboard.
        gov_ids = governing_internal_case_ids()
        ic = Case.objects.filter(
            case_type=CaseType.INTERNAL_SERVICE, case_id__in=gov_ids
        )
        ic_in_range = _scope_by_opened(ic, start, end)
        open_cases = ic_in_range.exclude(case_status__in=_TERMINAL_CASE_STATUSES)

        # --- 1.1 Open cases + authorization breakdown (TIME-FRAME SENSITIVE) ---
        # Mirrors the Members page "Eligible -> Internal Service = Open ->
        # Household / Individual" filter EXACTLY: ELIGIBLE members (lifecycle !=
        # INELIGIBLE) who have ANY non-terminal internal-service case (Exists) AND
        # an enrollment (own or household). Household vs Individual is read from
        # the ENROLLMENT program name (own or household enrollment), the same rule
        # the Members page uses -- NOT the case's program. Counted per member
        # (case holder); a member with no enrollment/program is excluded (neither
        # bucket), so household + individual == total.
        open_isc_sub = Case.objects.filter(
            client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
        ).exclude(case_status__in=_TERMINAL_CASE_STATUSES)
        if start is not None:  # time-frame: an ISC case OPENED in the window
            open_isc_sub = open_isc_sub.filter(
                date_opened__date__gte=start, date_opened__date__lte=end
            )
        eligible_open = (
            Client.objects.exclude(lifecycle_stage=ClientStage.INELIGIBLE)
            .filter(Exists(open_isc_sub))
        )
        household_prog = (
            Q(enrollments__program_name__icontains="household")
            | Q(household_membership__household__enrollment_verifications__program_name__icontains="household")
        )
        has_prog = (
            Q(enrollments__isnull=False)
            | Q(household_membership__household__enrollment_verifications__isnull=False)
        )
        hh_ids = set(eligible_open.filter(household_prog).values_list("client_id", flat=True))
        ind_ids = set(
            eligible_open.filter(has_prog).exclude(household_prog)
            .values_list("client_id", flat=True)
        )
        counted = hh_ids | ind_ids

        # Authorization bucket per counted member, from their GOVERNING case
        # (blank / never_requested never govern -> those fall to "other").
        _AUTH_BUCKET = {
            ServiceAuthorizationStatus.APPROVED: "accepted",
            ServiceAuthorizationStatus.PENDING: "requested",
            ServiceAuthorizationStatus.DENIED: "rejected",
        }
        gov_auth = dict(
            Case.objects.filter(case_id__in=gov_ids)
            .values_list("client_id", "service_authorization_status")
        )
        auth_counts = {"accepted": 0, "requested": 0, "rejected": 0, "other": 0}
        for cid in counted:
            auth_counts[_AUTH_BUCKET.get(gov_auth.get(cid), "other")] += 1

        open_cases_payload = {
            "total": len(counted),
            "household": len(hh_ids),
            "individual": len(ind_ids),
            "accepted": auth_counts["accepted"],
            "requested": auth_counts["requested"],
            "rejected": auth_counts["rejected"],
            "other": auth_counts["other"],
        }

        # --- 1.5 Meals vs Boxes (TIME-FRAME SENSITIVE) --------------------
        # Product mix across currently-open internal-service cases OPENED in the
        # selected date range (All Time => every open case).
        mb = {
            k: {"individual": 0, "household": 0}
            for k in ("meals", "boxes", "unknown")
        }
        all_open_rows = open_cases.values(
            "household_type",
            "program__product_type__type",
            "program_name",
            "service_type",
        )
        for row in all_open_rows:
            kind = _case_product_kind(
                row["program__product_type__type"],
                row["program_name"],
                row["service_type"],
            )
            kind_key = kind.value if kind is not None else "unknown"
            # Same program-name rule as the open-cases Household/Individual split
            # above (and the Cases export), not the stored household_type.
            hh_key = (
                "household"
                if "household" in (row["program_name"] or "").casefold()
                else "individual"
            )
            mb[kind_key][hh_key] += 1
        meals_boxes = {
            kind: {
                "individual": counts["individual"],
                "household": counts["household"],
                "total": counts["individual"] + counts["household"],
            }
            for kind, counts in mb.items()
        }

        # --- 1.2 Members across open cases (Primary vs Household) ----------
        # TIME-FRAME SENSITIVE: households of the counted open-case members above.
        members_payload = self._members_breakdown(counted)

        # --- 1.3 Total enrolled (ALL TIME, any status) --------------------
        enrolled_client_ids = set(ic.values_list("client_id", flat=True))
        total_enrolled = self._household_member_count(enrolled_client_ids)

        # --- 1.2b Unaffiliated members (TIME-FRAME SENSITIVE) -------------
        # Clients with an eligibility/navigation case OPENED in the selected
        # range who hold NO internal-service case and are not a member of any
        # household. Scoped by the eligibility/navigation case opened date (they
        # have no internal-service case to date-scope on). NOT part of the
        # members total above -- it's a separate, non-overlapping population.
        #
        # This must match the Urgent Care "Un-Linked Members" tab exactly, so we
        # additionally keep only those whose member id / Medicaid id is
        # referenced in ANOTHER member's case description -- using the same
        # helper that tab does (UnlinkedMembersListView._referenced_client_ids).
        from .views_members import UnlinkedMembersListView

        unaffiliated_qs = UnlinkedMembersListView.base_queryset(start, end)
        members_payload["unaffiliated"] = len(
            UnlinkedMembersListView._referenced_client_ids(unaffiliated_qs)
        )

        # --- 1.4 Active delivery members (ALL TIME) -----------------------
        # Members currently ACTIVE in a Service-Active enrollment, regardless
        # of when their case was opened.
        active_delivery_members = (
            MemberDietaryProfile.objects.filter(
                status=MemberStatus.ACTIVE,
                enrollment__stage=EnrollmentStage.SERVICE_ACTIVE,
            )
            .values("client_id")
            .distinct()
            .count()
        )

        # --- 1.6 Cancel rate (TIME-FRAME SENSITIVE) -----------------------
        # Attrition: distinct members who fell out of / are blocked from active
        # service (Paused, household On Hold, Out of Orbit, Out of Range, or a
        # Cancelled enrollment) as a share of the members enrolled in
        # accepted-authorization (open + APPROVED) cases.
        mdp = MemberDietaryProfile.objects

        def _lost(**flt):
            return (
                _scope_members(mdp.filter(**flt), start, end)
                .values("client_id").distinct().count()
            )

        cr_paused = _lost(status=MemberStatus.PAUSED)
        cr_on_hold = _lost(enrollment__stage=EnrollmentStage.ON_HOLD)
        cr_out_of_orbit = _lost(status=MemberStatus.OUT_OF_ORBIT)
        cr_out_of_range = _lost(status=MemberStatus.OUT_OF_RANGE)
        cr_cancelled = _lost(enrollment__stage=EnrollmentStage.CANCELLED)
        lost_total = (
            cr_paused + cr_on_hold + cr_out_of_orbit + cr_out_of_range + cr_cancelled
        )

        # Base: Total Members across open cases (the "Total Members" card's
        # value) -- the same in-range open-case member population.
        base_members = members_payload["total"]

        cancel_rate = {
            "paused": cr_paused,
            "on_hold": cr_on_hold,
            "out_of_orbit": cr_out_of_orbit,
            "out_of_range": cr_out_of_range,
            "cancelled": cr_cancelled,
            "lost_total": lost_total,
            "base": base_members,
            "rate": (
                round(lost_total / base_members * 100, 1)
                if base_members else 0.0
            ),
        }

        # --- Section 2: member serving-status breakdown --------------------
        # Distinct-member (client) counts. Each "Not Being Served" / watchlist
        # count is derived from serving_client_ids(), the same source of truth the
        # drill-down list endpoint uses, so a count can never disagree with its
        # list. pending_meals stays inline (it has no drill-down list of its own).
        # TIME-FRAME: the Not Being Served + Needs Follow-up counts are scoped to
        # the selected range (members tied to an internal-service case opened in
        # the window); All Time applies no date filter.
        mdp = MemberDietaryProfile.objects
        pending_meals = mdp.filter(
            enrollment__verified_at__isnull=False,
            enrollment__case__service_authorization_status=(
                ServiceAuthorizationStatus.PENDING
            ),
        )
        pending_meals = pending_meals.values("client_id").distinct().count()

        def _count(reason):
            return len(
                serving_client_ids(
                    reason, start=start, end=end, governing_ids=gov_ids
                )
            )

        serving = {
            # 2.1 Accepted case + Verified + being served.
            "receiving_meals": active_delivery_members,
            # 2.2 Verified but case authorization still Pending.
            "pending_meals": pending_meals,
            "not_being_served": {
                # 2.3a Awaiting verification.
                "needs_verification": _count("needs_verification"),
                # 2.3b Case authorization Denied.
                "rejected_case": _count("rejected_case"),
                # 2.3d Delivery/primary ZIP outside coverage (geographic block).
                "out_of_range": _count("out_of_range"),
                # 2.3e PROGRAM on hold (household-wide) vs programs with a member
                # paused -- split by who paused them (agent vs eligibility).
                "programs_on_hold": _count("programs_on_hold"),
                "members_paused_agent": _count("members_paused_agent"),
                "members_paused_eligibility": _count("members_paused_eligibility"),
                # 2.3f Closure initiated (open Case Closure ticket).
                "pending_closure": _count("pending_closure"),
                # 2.3g Dietary/allergy profile unfulfillable by any kitchen.
                "out_of_orbit": _count("out_of_orbit"),
            },
            # Follow-up watchlist: can overlap with actively-served members.
            "watchlist": {
                "insurance_expiring": _count("insurance_expiring"),
                "no_social_coverage": _count("no_social_coverage"),
            },
        }

        # --- Second-row service cards (TIME-FRAME SENSITIVE) --------------
        # Member-level (distinct client) breakdowns, scoped to members tied to an
        # internal-service case OPENED in the selected range (``_scope_members``
        # is a no-op for All Time, so these stay a live snapshot then). Leaf
        # counts are queried and the card TOTALS derived by summing them, so the
        # displayed math is always exactly additive and can never disagree with
        # its parts.
        def _members(**flt):
            return (
                _scope_members(mdp.filter(**flt), start, end)
                .values("client_id").distinct().count()
            )

        # Verification/authorization funnel counts (scoped to range) reused by
        # the Pending Meals card (MemberDietaryProfile-based serving snapshot).
        kitchen_assignment = _members(
            enrollment__stage=EnrollmentStage.KITCHEN_ASSIGNMENT
        )
        pending_verified_auth = _members(
            enrollment__verified_at__isnull=False,
            enrollment__case__service_authorization_status=(
                ServiceAuthorizationStatus.PENDING
            ),
        )
        active_members = _members(enrollment__stage=EnrollmentStage.SERVICE_ACTIVE)

        # Total Enrolled = Total Members across open cases MINUS the inactive
        # buckets (Paused + On Hold + Out of Orbit + Out of Range + Cancelled),
        # i.e. base_members - lost_total. The sub-rows break the enrolled members
        # down by verification funnel stage. They mirror the Verification page
        # EXACTLY -- same helpers, keyed on the verification FACT (verified_at),
        # scoped by the internal-service case CREATED date -- so an agent sees the
        # identical Pending / Verified split there (not additive to the headline;
        # these are funnel snapshots of the enrolled population).
        from .views_members import (
            apply_authorization_filter,
            apply_case_created_date_filter,
            require_internal_service_primary,
            verification_completed_q,
            verification_scope_q,
        )

        ver_base = apply_case_created_date_filter(
            require_internal_service_primary(
                Client.objects.filter(verification_scope_q())
            ),
            start,
            end,
        ).distinct()
        ver_verified = ver_base.filter(verification_completed_q())
        enrolled = {
            "pending_verification": (
                ver_base.exclude(verification_completed_q()).distinct().count()
            ),
            "verified_pending_auth": (
                apply_authorization_filter(ver_verified, "pending")
                .distinct().count()
            ),
            "kitchen_assignment": (
                ver_verified.filter(
                    Q(enrollments__stage=EnrollmentStage.KITCHEN_ASSIGNMENT)
                    | Q(
                        household_membership__household__enrollment_verifications__stage=(
                            EnrollmentStage.KITCHEN_ASSIGNMENT
                        )
                    )
                ).distinct().count()
            ),
            "active": active_members,
            "total": max(base_members - lost_total, 0),
        }

        # Total Receiving Meals: LIVE members we are actively serving in a PO --
        # status ACTIVE in a Service-Active enrollment. Deliberately EXCLUDES On
        # Hold / Paused / Cancelled / Out of Orbit / Out of Range (not currently
        # being served). Split DISJOINTLY (by distinct client) into members who
        # became active WITHIN the selected range ("new members added", tied to an
        # internal-service case opened in range) vs the all-history remainder, so
        # new + history == total exactly. All Time => new is 0 (no window).
        serving_flt = dict(
            status=MemberStatus.ACTIVE,
            enrollment__stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        serving_ids = set(
            mdp.filter(**serving_flt).values_list("client_id", flat=True)
        )
        new_serving_ids = (
            set()
            if start is None
            else set(
                _scope_members(mdp.filter(**serving_flt), start, end)
                .values_list("client_id", flat=True)
            )
        ) & serving_ids
        receiving_meals = {
            "new_members": len(new_serving_ids),
            "history": len(serving_ids - new_serving_ids),
            "total": len(serving_ids),
        }

        # Pending Meals: verified households not yet being served -- awaiting a
        # manual kitchen assignment (case auth approved), or still awaiting the
        # case authorization decision (verified, auth Pending). Scoped to range.
        pending = {
            "kitchen_assignment": kitchen_assignment,
            "verified_pending_auth": pending_verified_auth,
            "total": kitchen_assignment + pending_verified_auth,
        }

        return Response(
            {
                "period": "custom" if custom else period,
                "range": (
                    {"start": start.isoformat(), "end": end.isoformat()}
                    if start is not None
                    else None
                ),
                "open_cases": open_cases_payload,
                "members": members_payload,
                "total_enrolled": total_enrolled,
                "active_delivery_members": active_delivery_members,
                "meals_boxes": meals_boxes,
                "cancel_rate": cancel_rate,
                "serving": serving,
                "enrolled": enrolled,
                "receiving": receiving_meals,
                "pending": pending,
            }
        )

    @staticmethod
    def _household_member_count(case_client_ids):
        """Distinct clients across the households of the given case-holder
        clients (household heads included). Case holders without a household row
        still count as one member each."""
        if not case_client_ids:
            return 0
        household_ids = (
            HouseholdMember.objects.filter(client_id__in=case_client_ids)
            .values_list("household_id", flat=True)
            .distinct()
        )
        in_households = set(
            HouseholdMember.objects.filter(household_id__in=household_ids)
            .values_list("client_id", flat=True)
        )
        with_hh = set(
            HouseholdMember.objects.filter(client_id__in=case_client_ids)
            .values_list("client_id", flat=True)
        )
        orphans = set(case_client_ids) - with_hh  # case holders w/o a household
        return len(in_households | orphans)

    @staticmethod
    def _members_breakdown(case_client_ids):
        """Split the households of the given case-holder clients into Primary
        members (household heads) vs Members of Household. A case holder without
        a household row counts as a Primary member."""
        if not case_client_ids:
            return {"total": 0, "primary": 0, "household": 0}
        household_ids = (
            HouseholdMember.objects.filter(client_id__in=case_client_ids)
            .values_list("household_id", flat=True)
            .distinct()
        )
        members = HouseholdMember.objects.filter(household_id__in=household_ids)
        primary = members.filter(is_primary=True).values("client_id").distinct().count()
        household = members.filter(is_primary=False).values("client_id").distinct().count()
        with_hh = set(
            HouseholdMember.objects.filter(client_id__in=case_client_ids)
            .values_list("client_id", flat=True)
        )
        orphans = len(set(case_client_ids) - with_hh)
        primary += orphans
        return {
            "total": primary + household,
            "primary": primary,
            "household": household,
        }


class DashboardServingListView(PortalAPIView):
    """Management-only drill-down: the individual members behind one serving-status
    or watchlist ``reason`` (see :data:`_SERVING_REASONS`), scoped to the same date
    range as the summary. Each row names the flagged member, links to their
    household primary (where an agent acts), and carries a reason-specific detail.
    """

    def get(self, request, reason):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)
        if reason not in _SERVING_REASONS:
            return Response({"detail": "Unknown reason."}, status=404)

        # Scope the drill-down to the SAME date range as the dashboard cards
        # (Not Being Served / Needs Follow-up are range-scoped), so a count and
        # its member list can never disagree. All Time => no date filter.
        start, end = resolve_window(request)
        # Same governing-case restriction as the summary counts, so the list and
        # its count can never disagree (and no non-governing case is considered).
        gov_ids = governing_internal_case_ids()
        client_ids = serving_client_ids(
            reason, start=start, end=end, governing_ids=gov_ids
        ) or set()
        details = _serving_details(
            reason, client_ids, start=start, end=end, governing_ids=gov_ids
        )
        primary_map = _primary_map(client_ids)

        needed = set(client_ids) | set(primary_map.values())
        names = {
            c.client_id: (f"{c.first_name} {c.last_name}".strip() or str(c.client_id))
            for c in Client.objects.filter(client_id__in=needed)
        }

        results = []
        for cid in client_ids:
            pid = primary_map.get(cid, cid)
            results.append({
                "id": str(cid),
                "name": names.get(cid, str(cid)),
                "primary_id": str(pid),
                "primary_name": names.get(pid, names.get(cid, str(cid))),
                "detail": details.get(cid, ""),
            })
        results.sort(key=lambda r: r["name"].lower())

        return Response({"reason": reason, "count": len(results), "results": results})

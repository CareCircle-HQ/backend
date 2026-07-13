"""Management dashboard analytics.

A single aggregate endpoint (management-only) reporting on the internal-service
(meal/box) program. Only three metrics are scoped by the selected date range (to
internal-service cases OPENED within that window): Total Open Cases, Total
Members, and Cancel Rate. Every other metric -- Total Enrolled, Active Delivery
Members, Meals vs Boxes, and the whole Section-2 serving breakdown -- is an
all-time live snapshot regardless of the selected range. ``All Time`` applies no
date filter to any metric. See :class:`DashboardView` for the exact definitions.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    Case,
    CaseHouseholdType,
    CaseStatus,
    CaseType,
    Client,
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

# Medicaid insurance within this many days of its end date is "expiring soon".
_EXPIRING_WINDOW_DAYS = 30

# The drill-down reasons the serving-status list endpoint understands. The first
# group mirrors the "Not Being Served" cards; the second is the follow-up
# watchlist (which can overlap with actively-served members).
_SERVING_REASONS = frozenset({
    "needs_verification", "rejected_case", "multiple_cases", "out_of_range",
    "services_paused", "pending_closure", "out_of_orbit",
    "insurance_expiring", "no_social_coverage", "no_insurance",
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


def _scope_by_opened(qs, start, end):
    """Restrict a Case queryset to rows OPENED within [start, end]. No-op when
    the window is open-ended (all time). Rows with a NULL ``date_opened`` are
    dropped only when a window is applied."""
    if start is None:
        return qs
    return qs.filter(date_opened__date__gte=start, date_opened__date__lte=end)


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


def serving_client_ids(reason, *, start, end):
    """Distinct client_ids for a serving-status / watchlist ``reason``, scoped to
    the selected date range. Single source of truth for both the dashboard counts
    and the drill-down list, so the two never disagree on WHO is included.

    Returns a set of client_ids, or ``None`` for an unknown reason.
    """
    mdp = MemberDietaryProfile.objects

    def scope(qs):
        return _scope_members(qs, start, end)

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
        return set(scope(mdp.filter(
            enrollment__case__service_authorization_status=(
                ServiceAuthorizationStatus.DENIED
            )
        )).values_list("client_id", flat=True))
    if reason == "out_of_range":
        return set(scope(
            mdp.filter(status=MemberStatus.OUT_OF_RANGE)
        ).values_list("client_id", flat=True))
    if reason == "out_of_orbit":
        return set(scope(
            mdp.filter(status=MemberStatus.OUT_OF_ORBIT)
        ).values_list("client_id", flat=True))
    if reason == "services_paused":
        # Member-level Pause (benign) OR household ON_HOLD (problem/review).
        return set(scope(mdp.filter(
            Q(status=MemberStatus.PAUSED)
            | Q(enrollment__stage=EnrollmentStage.ON_HOLD)
        )).values_list("client_id", flat=True))
    if reason == "multiple_cases":
        open_cases = _scope_by_opened(
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE), start, end
        ).exclude(case_status__in=_TERMINAL_CASE_STATUSES)
        return {
            row["client_id"]
            for row in open_cases.values("client_id")
            .annotate(n=Count("case_id"))
            .filter(n__gt=1)
        }
    if reason == "pending_closure":
        tickets = Ticket.objects.filter(
            type__code=TicketTypeCode.CASE_CLOSURE,
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

    # --- Watchlist (client-level facts, intersected with enrolled members) ---
    enrolled = set(scope(mdp).values_list("client_id", flat=True))
    if not enrolled and start is not None:
        return set()
    if reason == "insurance_expiring":
        window = timezone.now() + timedelta(days=_EXPIRING_WINDOW_DAYS)
        return set(Insurance.objects.filter(
            client_id__in=enrolled,
            plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE,
            expired_at__isnull=False,
            expired_at__lte=window,
        ).values_list("client_id", flat=True))
    if reason == "no_social_coverage":
        have = set(SocialCareCoverage.objects.filter(
            client_id__in=enrolled,
            status=SocialCareCoverageStatus.ENROLLED,
        ).values_list("client_id", flat=True))
        return enrolled - have
    if reason == "no_insurance":
        have = set(Insurance.objects.filter(
            client_id__in=enrolled, status=RecordStatus.ACTIVE,
        ).values_list("client_id", flat=True))
        return enrolled - have
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


def _serving_details(reason, client_ids, *, start, end):
    """Per-client presentation detail for a reason's list rows. Presentation only
    -- membership is decided by :func:`serving_client_ids`."""
    if not client_ids:
        return {}
    ids = set(client_ids)
    mdp = MemberDietaryProfile.objects

    if reason in ("needs_verification", "rejected_case"):
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
    if reason == "services_paused":
        out = {}
        for p in (
            mdp.filter(client_id__in=ids)
            .select_related("enrollment")
            .order_by("client_id")
        ):
            if p.client_id in out:
                continue
            if p.status == MemberStatus.PAUSED:
                since = _fmt_date(p.status_changed_at)
                out[p.client_id] = f"Paused{f' · since {since}' if since else ''}"
            elif getattr(p.enrollment, "stage", None) == EnrollmentStage.ON_HOLD:
                out[p.client_id] = "Household on hold (under review)"
        return out
    if reason == "multiple_cases":
        open_cases = _scope_by_opened(
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE), start, end
        ).exclude(case_status__in=_TERMINAL_CASE_STATUSES)
        by_client = {}
        for row in open_cases.filter(client_id__in=ids).values(
            "client_id", "program_name", "service_type"
        ):
            name = (row["program_name"] or row["service_type"] or "Case").strip()
            by_client.setdefault(row["client_id"], []).append(name)
        return {
            cid: f"{len(names)} open cases: " + ", ".join(names[:4])
            for cid, names in by_client.items()
        }
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
    if reason == "insurance_expiring":
        window = timezone.now() + timedelta(days=_EXPIRING_WINDOW_DAYS)
        plans = Insurance.objects.filter(
            client_id__in=ids,
            plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE,
            expired_at__isnull=False,
            expired_at__lte=window,
        ).order_by("client_id", "expired_at")
        out = {}
        for pl in plans:
            if pl.client_id in out:
                continue
            plan = (pl.plan_name or "Medicaid").strip()
            out[pl.client_id] = f"{plan} · expires {_fmt_date(pl.expired_at)}"
        return out
    if reason == "no_social_coverage":
        return {cid: "No enrolled social care coverage" for cid in ids}
    if reason == "no_insurance":
        return {cid: "No active insurance on file" for cid in ids}
    return {}


class DashboardView(PortalAPIView):
    """Management-only program dashboard.

    Three metrics are TIME-FRAME SENSITIVE -- scoped to Internal Service cases
    OPENED within the selected date range (``period``; ``all`` applies no date
    filter): open_cases, members, and cancel_rate. Every other metric is an
    ALL-TIME live snapshot. Metric definitions:

    * open_cases  -- [TIME-FRAME] non-terminal (not Closed/Cancelled)
      internal-service cases, broken down by service-authorization outcome:
      Accepted (approved), Requested (pending), Rejected (denied).
    * members     -- [TIME-FRAME] distinct clients across those open cases'
      households, split into Primary members (household heads / case holders)
      vs Members of Household. A client with several open cases counts once.
    * cancel_rate -- [TIME-FRAME] (closed + cancelled + on-hold) internal-service
      cases as a proportion of all in-range internal-service cases opened.
      On-hold counts here because it flags a problem case under review that may
      close (distinct from a benign, temporary member-level Pause).
    * total_enrolled -- [ALL TIME] distinct members in the households of ALL
      internal-service cases, regardless of status.
    * active_delivery_members -- [ALL TIME] distinct members currently ACTIVE in
      a Service-Active enrollment (Accepted + Verified + being served).
    * meals_boxes -- [ALL TIME] currently-open cases split by product kind
      (Meals/Boxes) and, within each, Individual vs Household.
    * serving -- [ALL TIME] Section-2 member serving-status breakdown.
    """

    def get(self, request):
        # --- Management-only gate -----------------------------------------
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        period = (request.query_params.get("period") or "all").lower()
        start, end = period_window(period)

        ic = Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
        ic_in_range = _scope_by_opened(ic, start, end)
        open_cases = ic_in_range.exclude(case_status__in=_TERMINAL_CASE_STATUSES)

        # --- 1.1 Open cases + authorization breakdown (TIME-FRAME SENSITIVE)
        # Scoped to cases opened in the selected date range.
        scoped_rows = list(
            open_cases.values("client_id", "service_authorization_status")
        )
        auth_counts = {"accepted": 0, "requested": 0, "rejected": 0, "other": 0}
        _AUTH_BUCKET = {
            ServiceAuthorizationStatus.APPROVED: "accepted",
            ServiceAuthorizationStatus.PENDING: "requested",
            ServiceAuthorizationStatus.DENIED: "rejected",
        }
        for row in scoped_rows:
            auth_counts[_AUTH_BUCKET.get(row["service_authorization_status"], "other")] += 1

        open_cases_payload = {
            "total": len(scoped_rows),
            "accepted": auth_counts["accepted"],
            "requested": auth_counts["requested"],
            "rejected": auth_counts["rejected"],
            "other": auth_counts["other"],
        }

        # --- 1.5 Meals vs Boxes (ALL TIME) --------------------------------
        # Product mix across EVERY currently-open internal-service case,
        # regardless of the selected date range.
        mb = {
            k: {"individual": 0, "household": 0}
            for k in ("meals", "boxes", "unknown")
        }
        all_open_rows = ic.exclude(case_status__in=_TERMINAL_CASE_STATUSES).values(
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
            hh_key = (
                "household"
                if row["household_type"] == CaseHouseholdType.HOUSEHOLD
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
        # TIME-FRAME SENSITIVE: households of the in-range open cases above.
        open_client_ids = {row["client_id"] for row in scoped_rows}
        members_payload = self._members_breakdown(open_client_ids)

        # --- 1.3 Total enrolled (ALL TIME, any status) --------------------
        enrolled_client_ids = set(ic.values_list("client_id", flat=True))
        total_enrolled = self._household_member_count(enrolled_client_ids)

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

        # --- 1.6 Cancel / churn rate --------------------------------------
        total_opened = ic_in_range.count()
        closed = ic_in_range.filter(case_status=CaseStatus.CLOSED).count()
        cancelled = ic_in_range.filter(case_status=CaseStatus.CANCELLED).count()
        paused = (
            ic_in_range.exclude(case_status__in=_TERMINAL_CASE_STATUSES)
            .filter(enrollments__stage=EnrollmentStage.ON_HOLD)
            .distinct()
            .count()
        )
        churned = closed + cancelled + paused
        cancel_rate = {
            "closed": closed,
            "cancelled": cancelled,
            "paused": paused,
            "churned": churned,
            "total_opened": total_opened,
            "rate": round(churned / total_opened * 100, 1) if total_opened else 0.0,
        }

        # --- Section 2: member serving-status breakdown --------------------
        # Distinct-member (client) counts. Each "Not Being Served" / watchlist
        # count is derived from serving_client_ids(), the same source of truth the
        # drill-down list endpoint uses, so a count can never disagree with its
        # list. pending_meals stays inline (it has no drill-down list of its own).
        # ALL TIME: Section-2 counts are live snapshots, ignoring the range.
        mdp = MemberDietaryProfile.objects
        pending_meals = mdp.filter(
            enrollment__verified_at__isnull=False,
            enrollment__case__service_authorization_status=(
                ServiceAuthorizationStatus.PENDING
            ),
        )
        pending_meals = pending_meals.values("client_id").distinct().count()

        def _count(reason):
            return len(serving_client_ids(reason, start=None, end=None))

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
                # 2.3c >1 open internal-service case (surfaced for manual review).
                "multiple_cases": _count("multiple_cases"),
                # 2.3d Delivery/primary ZIP outside coverage (geographic block).
                "out_of_range": _count("out_of_range"),
                # 2.3e Member Paused (benign) OR household On Hold (under review).
                "services_paused": _count("services_paused"),
                # 2.3f Closure initiated (open Case Closure ticket).
                "pending_closure": _count("pending_closure"),
                # 2.3g Dietary/allergy profile unfulfillable by any kitchen.
                "out_of_orbit": _count("out_of_orbit"),
            },
            # Follow-up watchlist: can overlap with actively-served members.
            "watchlist": {
                "insurance_expiring": _count("insurance_expiring"),
                "no_social_coverage": _count("no_social_coverage"),
                "no_insurance": _count("no_insurance"),
            },
        }

        # --- Second-row service cards (ALL TIME) --------------------------
        # Member-level (distinct client) breakdowns. Leaf counts are queried and
        # the card TOTALS derived by summing them, so the displayed math is
        # always exactly additive and can never disagree with its parts.
        def _members(**flt):
            return mdp.filter(**flt).values("client_id").distinct().count()

        _ACTIVE_ENR = [EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.ON_HOLD]

        # Receiving Meals: status ACTIVE in a live (Service Active) enrollment.
        receiving = active_delivery_members
        # Still in the active-service pipeline but not currently receiving:
        # individually Paused, or the whole household is On Hold (members keep
        # ACTIVE status while the enrollment sits at On Hold).
        paused = _members(status=MemberStatus.PAUSED, enrollment__stage__in=_ACTIVE_ENR)
        on_hold = _members(
            status=MemberStatus.ACTIVE, enrollment__stage=EnrollmentStage.ON_HOLD
        )
        active_pipeline = receiving + paused + on_hold

        # Total Enrolled: the active-delivery pipeline plus the two delivery-
        # blocked buckets (dietary Out of Orbit, geographic Out of Range).
        enrolled = {
            "active": active_pipeline,
            "out_of_orbit": _count("out_of_orbit"),
            "out_of_range": _count("out_of_range"),
        }
        enrolled["total"] = (
            enrolled["active"] + enrolled["out_of_orbit"] + enrolled["out_of_range"]
        )

        receiving_meals = {
            "active": active_pipeline,
            "paused": paused,
            "on_hold": on_hold,
            "total": receiving,  # active_pipeline - paused - on_hold
        }

        # Pending Meals: verified households not yet being served -- awaiting a
        # manual kitchen assignment (case auth approved), or still awaiting the
        # case authorization decision (verified, auth Pending).
        kitchen_assignment = _members(
            enrollment__stage=EnrollmentStage.KITCHEN_ASSIGNMENT
        )
        pending = {
            "kitchen_assignment": kitchen_assignment,
            "verified_pending_auth": pending_meals,
            "total": kitchen_assignment + pending_meals,
        }

        return Response(
            {
                "period": period,
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

        # Section-2 serving metrics are all-time (they mirror the dashboard
        # cards, which ignore the selected range), so the drill-down is too.
        start, end = None, None
        client_ids = serving_client_ids(reason, start=start, end=end) or set()
        details = _serving_details(reason, client_ids, start=start, end=end)
        # multiple_cases links to the member's own profile (it is about that
        # member's duplicate cases); every other reason links to the primary.
        primary_map = (
            {cid: cid for cid in client_ids}
            if reason == "multiple_cases"
            else _primary_map(client_ids)
        )

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

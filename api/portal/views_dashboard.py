"""Dashboard analytics: a single aggregate endpoint for the backed widgets.

Backed: leads funnel, opportunity conversion, tickets KPIs, new enrollments,
ticket activity trend, case activity, coverage-by-area. (Time-to-convert,
pending renewals and the insurance distribution stay static on the frontend.)
"""

from datetime import timedelta

from django.db.models import Count, Max, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    Address,
    Case,
    Client,
    ClientStage,
    DeliveryOrder,
    EnrollmentStage,
    EnrollmentVerification,
    ProductTypeKind,
    PurchaseOrder,
    PurchaseOrderStatus,
    StageEntityType,
    StageEvent,
    Ticket,
)
from .base import PortalAPIView

# Cumulative lifecycle ranks for funnel math.
STAGE_RANK = {
    "inactive": 0, "consent": 1, "screened": 2, "assessment": 3, "navigation": 4,
    "pending_verification": 5, "verified": 6, "waiting_authorization": 7,
    "authorized": 8, "kitchen_assignment": 9, "active": 10, "completed": 11,
}

# The Conversion Funnel bars, in order, each mapped to the MINIMUM lifecycle
# rank a client must have reached to be counted in that (cumulative) bar. The
# internal-service-case holder is the household's primary "member"; leads and
# dependents without an internal-service case are carried by their household's
# stage.
FUNNEL_BARS = [
    ("Consent", STAGE_RANK["consent"]),
    ("Screening", STAGE_RANK["screened"]),
    ("Eligibility", STAGE_RANK["assessment"]),
    ("Internal Service Case", STAGE_RANK["navigation"]),
    ("Verification", STAGE_RANK["pending_verification"]),
    ("Kitchen Assignment", STAGE_RANK["kitchen_assignment"]),
    ("Active", STAGE_RANK["active"]),
    ("Completed", STAGE_RANK["completed"]),
]


def period_window(period):
    """Map a dashboard period code to an inclusive (start, end) date window on
    the LOCAL calendar. Weeks start Monday; current periods end today. Defaults
    to "today". ``last_month`` is the previous calendar month.
    """
    period = (period or "").strip().lower()
    today = timezone.localdate()
    if period == "week":  # This Week
        return today - timedelta(days=today.weekday()), today
    if period == "month":  # This Month
        return today.replace(day=1), today
    if period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == "year":  # This Year
        return today.replace(month=1, day=1), today
    return today, today  # today (default)


def _at_least(clients_by_rank, rank):
    return sum(n for r, n in clients_by_rank.items() if r >= rank)


class DashboardView(PortalAPIView):
    def get(self, request):
        period = (request.query_params.get("period") or "today").lower()
        win_start, win_end = period_window(period)
        now = timezone.now()

        # --- Funnel (cumulative lifecycle counts, split by household size) ---
        # Each client is bucketed by household composition: a "multiple-member"
        # household (>1 member) vs "single" (a one-member household or an
        # ungrouped individual). Bars are cumulative -- a client counts toward
        # every bar at or below the stage they've reached.
        rows = (
            Client.objects.exclude(lifecycle_stage="not_eligible")
            .annotate(hh=Count("household_membership__household__members", distinct=True))
            .values_list("lifecycle_stage", "hh")
        )
        single_by_rank, multi_by_rank = {}, {}
        for stage, hh in rows:
            rank = STAGE_RANK.get(stage, 0)
            bucket = multi_by_rank if (hh or 0) > 1 else single_by_rank
            bucket[rank] = bucket.get(rank, 0) + 1

        stages = []
        for name, rank in FUNNEL_BARS:
            s = _at_least(single_by_rank, rank)
            m = _at_least(multi_by_rank, rank)
            stages.append({"name": name, "single": s, "multi": m, "value": s + m})

        by_name = {st["name"]: st["value"] for st in stages}
        consent = by_name["Consent"]
        screening = by_name["Screening"]
        eligible = by_name["Eligibility"]
        verified_plus = by_name["Verification"]
        converted = by_name["Active"]  # cumulative: reached >= Active

        def pct(num, den):
            return round(num / den * 100) if den else 0

        funnel = {
            "stages": stages,
            "rates": {
                "consent_to_screening": pct(screening, consent),
                "screening_to_eligible": pct(eligible, screening),
                "eligible_to_converted": pct(converted, eligible),
            },
        }
        opportunity = {
            "verify_to_member": pct(converted, verified_plus),
            "leads_to_members": pct(converted, consent),
        }

        # --- Tickets KPIs ---
        tq = Ticket.objects.all()
        tickets = {
            "new": tq.filter(
                created_at__date__gte=win_start, created_at__date__lte=win_end
            ).count(),
            "open": tq.filter(status="open").count(),
            "in_progress": tq.filter(status="in_progress").count(),
            "resolved": tq.filter(status="resolved").count(),
        }

        # --- New enrollments in period ---
        new_enrollments = EnrollmentVerification.objects.filter(
            opened_at__date__gte=win_start, opened_at__date__lte=win_end
        ).count()

        # --- Member metrics (Members-focused dashboard) --------------------
        # Period-scoped counts use the selected [win_start, win_end] window;
        # the "serving" totals are LIVE snapshots of the most recent delivery
        # week (independent of the period selector).
        requested_verifications = EnrollmentVerification.objects.filter(
            opened_at__date__gte=win_start, opened_at__date__lte=win_end
        ).count()
        verifications_done = EnrollmentVerification.objects.filter(
            verified_at__date__gte=win_start, verified_at__date__lte=win_end
        ).count()
        # Members who transitioned INTO Active within the window (append-only
        # StageEvent audit log; distinct members in case of repeat events).
        became_active = (
            StageEvent.objects.filter(
                entity_type=StageEntityType.CLIENT,
                to_stage=ClientStage.ACTIVE,
                entered_at__date__gte=win_start,
                entered_at__date__lte=win_end,
            )
            .values("client")
            .distinct()
            .count()
        )
        # Members currently awaiting a kitchen assignment (live count).
        waiting_kitchen_assignment = Client.objects.filter(
            lifecycle_stage=ClientStage.KITCHEN_ASSIGNMENT
        ).count()

        # "Actually served on the last PO": all non-cancelled delivery orders
        # whose PO delivery_date falls in the most recent delivery WEEK (Mon-Sun
        # containing the latest delivery_date). Meals vs boxes split by PO kind.
        last_po_date = (
            PurchaseOrder.objects.exclude(status=PurchaseOrderStatus.CANCELLED)
            .filter(delivery_date__isnull=False)
            .aggregate(m=Max("delivery_date"))["m"]
        )
        active_serving = meals_served = boxes_served = 0
        last_delivery_week = None
        if last_po_date is not None:
            wk_start = last_po_date - timedelta(days=last_po_date.weekday())
            wk_end = wk_start + timedelta(days=6)
            last_delivery_week = {"start": wk_start, "end": wk_end}
            served = DeliveryOrder.objects.filter(
                purchase_order__delivery_date__gte=wk_start,
                purchase_order__delivery_date__lte=wk_end,
            ).exclude(purchase_order__status=PurchaseOrderStatus.CANCELLED)
            active_serving = (
                served.filter(member__isnull=False).values("member").distinct().count()
            )
            meals_served = (
                served.filter(purchase_order__kind=ProductTypeKind.MEALS).aggregate(
                    s=Sum("quantity")
                )["s"]
                or 0
            )
            boxes_served = (
                served.filter(purchase_order__kind=ProductTypeKind.BOXES).aggregate(
                    s=Sum("quantity")
                )["s"]
                or 0
            )
        members = {
            "requested_verifications": requested_verifications,
            "verifications_done": verifications_done,
            "became_active": became_active,
            "waiting_kitchen_assignment": waiting_kitchen_assignment,
            "active_serving": active_serving,
            "meals_served": meals_served,
            "boxes_served": boxes_served,
            "last_delivery_week": last_delivery_week,
        }

        # --- Ticket activity trend (8 weeks: open created vs resolved) ---
        trend = []
        for i in range(7, -1, -1):
            wk_start = now - timedelta(weeks=i + 1)
            wk_end = now - timedelta(weeks=i)
            trend.append(
                {
                    "week": wk_end.strftime("%b %d"),
                    "open": tq.filter(created_at__gte=wk_start, created_at__lt=wk_end).count(),
                    "resolved": tq.filter(
                        resolved_at__gte=wk_start, resolved_at__lt=wk_end
                    ).count(),
                }
            )

        # --- Case activity (last 6 months) ---
        six_months = now - timedelta(days=183)
        created_by_month = dict(
            Case.objects.filter(date_opened__gte=six_months)
            .annotate(m=TruncMonth("date_opened"))
            .values_list("m")
            .annotate(n=Count("case_id"))
        )
        closed_by_month = dict(
            Case.objects.filter(case_closed_at__gte=six_months)
            .annotate(m=TruncMonth("case_closed_at"))
            .values_list("m")
            .annotate(n=Count("case_id"))
        )
        case_activity = []
        for i in range(5, -1, -1):
            month = (now - timedelta(days=30 * i)).replace(day=1)
            key = next(
                (k for k in created_by_month if k and k.year == month.year and k.month == month.month),
                None,
            )
            ckey = next(
                (k for k in closed_by_month if k and k.year == month.year and k.month == month.month),
                None,
            )
            case_activity.append(
                {
                    "month": month.strftime("%b"),
                    "created": created_by_month.get(key, 0),
                    "closed": closed_by_month.get(ckey, 0),
                }
            )
        case_stats = {
            "created": Case.objects.filter(date_opened__gte=six_months).count(),
            "closed": Case.objects.filter(case_closed_at__gte=six_months).count(),
            "auth_pending": Case.objects.filter(case_status="pending_authorization").count(),
        }

        # --- Coverage by area (top current-address cities) ---
        coverage_by_area = [
            {"area": row["city"] or "Unknown", "members": row["n"]}
            for row in (
                Address.objects.filter(type="current")
                .exclude(city="")
                .values("city")
                .annotate(n=Count("client", distinct=True))
                .order_by("-n")[:6]
            )
        ]

        return Response(
            {
                "period": period,
                "funnel": funnel,
                "opportunity": opportunity,
                "members": members,
                "tickets": tickets,
                "new_enrollments": new_enrollments,
                "ticket_trend": trend,
                "case_activity": case_activity,
                "case_stats": case_stats,
                "coverage_by_area": coverage_by_area,
            }
        )

"""Dashboard analytics: a single aggregate endpoint for the backed widgets.

Backed: leads funnel, opportunity conversion, tickets KPIs, new enrollments,
ticket activity trend, case activity, coverage-by-area. (Time-to-convert,
pending renewals and the insurance distribution stay static on the frontend.)
"""

from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.response import Response

from ..models import Address, Case, Client, EnrollmentVerification, Ticket
from .base import PortalAPIView

# Cumulative lifecycle ranks for funnel math.
STAGE_RANK = {
    "inactive": 0, "consent": 1, "screened": 2, "assessment": 3, "navigation": 4,
    "pending_verification": 5, "verified": 6, "waiting_authorization": 7,
    "authorized": 8, "active": 9, "completed": 10,
}
CONVERTED_STAGES = ("active", "completed")


def _period_start(period):
    now = timezone.now()
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "year":
        return now - timedelta(days=365)
    return now - timedelta(days=30)  # month (default)


def _at_least(clients_by_rank, rank):
    return sum(n for r, n in clients_by_rank.items() if r >= rank)


class DashboardView(PortalAPIView):
    def get(self, request):
        period = (request.query_params.get("period") or "month").lower()
        start = _period_start(period)
        now = timezone.now()

        # --- Funnel (cumulative lifecycle counts, excluding not_eligible) ---
        stage_counts = dict(
            Client.objects.exclude(lifecycle_stage="not_eligible")
            .values_list("lifecycle_stage")
            .annotate(n=Count("client_id"))
        )
        by_rank = {}
        for stage, n in stage_counts.items():
            by_rank[STAGE_RANK.get(stage, 0)] = by_rank.get(STAGE_RANK.get(stage, 0), 0) + n

        consent = _at_least(by_rank, STAGE_RANK["consent"])
        screening = _at_least(by_rank, STAGE_RANK["screened"])
        eligible = _at_least(by_rank, STAGE_RANK["assessment"])
        converted = Client.objects.filter(lifecycle_stage__in=CONVERTED_STAGES).count()
        verified_plus = _at_least(by_rank, STAGE_RANK["verified"])

        def pct(num, den):
            return round(num / den * 100) if den else 0

        funnel = {
            "stages": [
                {"name": "Consent", "value": consent},
                {"name": "Screening", "value": screening},
                {"name": "Eligible", "value": eligible},
                {"name": "Converted", "value": converted},
            ],
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
            "new": tq.filter(created_at__gte=start).count(),
            "open": tq.filter(status="open").count(),
            "in_progress": tq.filter(status="in_progress").count(),
            "resolved": tq.filter(status="resolved").count(),
        }

        # --- New enrollments in period ---
        new_enrollments = EnrollmentVerification.objects.filter(
            opened_at__gte=start
        ).count()

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
                "tickets": tickets,
                "new_enrollments": new_enrollments,
                "ticket_trend": trend,
                "case_activity": case_activity,
                "case_stats": case_stats,
                "coverage_by_area": coverage_by_area,
            }
        )

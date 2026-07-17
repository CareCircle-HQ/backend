"""Customer Service dashboard: aggregate metrics for the CS group + managers.

Three read-only endpoints back the CS Dashboard page:

* :class:`CSDashboardSummaryView`  -- ``GET /portal/cs-dashboard/``
    Act-now triage (Care Management queue), delivery-coverage counts,
    verification backlog, ticket counts, and the calling agent's personal slice.
    Available to CS + Management (and manager override).

* :class:`CSDashboardTrendsView`   -- ``GET /portal/cs-dashboard/trends/?days=30``
    Daily opened-vs-resolved series for warnings and tickets, plus ticket
    resolution-time summary. CS + Management.

* :class:`CSTicketManagerStatsView` -- ``GET /portal/cs-dashboard/ticket-stats/``
    Manager-facing ticket analytics: backlog, aging, breakdowns by
    type/severity/source/origin, and solved-by-agent. Management only.

Everything reads existing tables (the ``MemberWarning`` snapshot, ``Ticket``,
``EnrollmentVerification``, ``Agent``) so the endpoints stay cheap and require no
new tracking.
"""

import statistics
from datetime import datetime, timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.response import Response

from api.models import (
    Agent,
    EnrollmentStage,
    EnrollmentVerification,
    MemberWarning,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    Ticket,
    TicketStatus,
    TicketType,
    WarningSeverity,
    WarningStatus,
)
from api.services.warnings import (
    CARE_MANAGEMENT_CODES,
    HOUSEHOLD_MEMBERS_OUT_OF_ORBIT,
    HOUSEHOLD_MEMBERS_OUT_OF_RANGE,
    HOUSEHOLD_MEMBERS_PAUSED,
    HOUSEHOLD_ON_HOLD,
)
from .base import PortalAPIView, current_agent

_CS_GROUPS = ("CS", "Management")
_OPEN_TICKET_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)


# ── access helpers ──────────────────────────────────────────────────────────
def _is_cs(agent):
    if not agent:
        return False
    return agent.group in _CS_GROUPS or getattr(agent, "is_manager", False)


def _is_manager(agent):
    if not agent:
        return False
    return agent.group == "Management" or getattr(agent, "is_manager", False)


def _parse_date(value):
    """Parse a YYYY-MM-DD (or common US) date; None on blank/failure."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _age_days(when, now):
    """Whole days between ``when`` and ``now`` (>= 0)."""
    if when is None:
        return 0
    return max(0, int((now - when).total_seconds() // 86400))


def _bucket_age(age_days):
    """Aging bucket key for an age in days."""
    if age_days < 1:
        return "d0_1"
    if age_days < 3:
        return "d1_3"
    if age_days < 7:
        return "d3_7"
    return "d7_plus"


def _empty_buckets():
    return {"d0_1": 0, "d1_3": 0, "d3_7": 0, "d7_plus": 0}


# ── shared aggregations ─────────────────────────────────────────────────────
def _care_management_summary(now):
    """Households on the Care Management queue (actionable service-config
    warnings on served households), grouped like the queue view. Returns
    counts, per-code breakdown, aging buckets and the unassigned-kitchen count."""
    rows = (
        MemberWarning.objects.filter(
            status=WarningStatus.ACTIVE, code__in=CARE_MANAGEMENT_CODES
        )
        .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        .values(
            "code",
            "severity",
            "enrollment_id",
            "client_id",
            "first_detected_at",
            "enrollment__kitchen_id",
        )
    )
    households = {}
    by_code = {}
    for r in rows:
        by_code[r["code"]] = by_code.get(r["code"], 0) + 1
        key = r["enrollment_id"] or f"client:{r['client_id']}"
        hh = households.get(key)
        if hh is None:
            hh = households[key] = {
                "max_rank": 0,
                "first": r["first_detected_at"],
                "kitchen_id": r["enrollment__kitchen_id"],
            }
        rank = 2 if r["severity"] == WarningSeverity.RED else 1
        hh["max_rank"] = max(hh["max_rank"], rank)
        if r["first_detected_at"] and (
            hh["first"] is None or r["first_detected_at"] < hh["first"]
        ):
            hh["first"] = r["first_detected_at"]

    total = len(households)
    red = sum(1 for h in households.values() if h["max_rank"] == 2)
    aging = _empty_buckets()
    unassigned_kitchen = 0
    for h in households.values():
        aging[_bucket_age(_age_days(h["first"], now))] += 1
        if h["kitchen_id"] is None:
            unassigned_kitchen += 1

    return {
        "households": total,
        "red": red,
        "orange": total - red,
        "unassigned_kitchen": unassigned_kitchen,
        "by_code": by_code,
        "aging": aging,
    }


def _coverage_counts():
    """Active informational-state warning counts CS monitors (not on the
    remediation queue but useful at-a-glance)."""
    codes = [
        HOUSEHOLD_MEMBERS_OUT_OF_ORBIT,
        HOUSEHOLD_MEMBERS_OUT_OF_RANGE,
        HOUSEHOLD_MEMBERS_PAUSED,
        HOUSEHOLD_ON_HOLD,
    ]
    counts = {c: 0 for c in codes}
    rows = (
        MemberWarning.objects.filter(status=WarningStatus.ACTIVE, code__in=codes)
        .values("code")
        .annotate(n=Count("id"))
    )
    for r in rows:
        counts[r["code"]] = r["n"]
    return {
        "out_of_orbit": counts[HOUSEHOLD_MEMBERS_OUT_OF_ORBIT],
        "out_of_range": counts[HOUSEHOLD_MEMBERS_OUT_OF_RANGE],
        "paused": counts[HOUSEHOLD_MEMBERS_PAUSED],
        "on_hold": counts[HOUSEHOLD_ON_HOLD],
    }


def _verification_summary(now):
    """Pending-verification enrollment backlog + aging (by opened_at)."""
    qs = EnrollmentVerification.objects.filter(
        stage=EnrollmentStage.PENDING_VERIFICATION
    ).values_list("opened_at", flat=True)
    aging = _empty_buckets()
    total = 0
    oldest_days = 0
    for opened_at in qs:
        total += 1
        age = _age_days(opened_at, now)
        aging[_bucket_age(age)] += 1
        oldest_days = max(oldest_days, age)
    return {"pending": total, "aging": aging, "oldest_days": oldest_days}


def _ticket_counts():
    qs = Ticket.objects.all()
    return {
        "open": qs.filter(status=TicketStatus.OPEN).count(),
        "in_progress": qs.filter(status=TicketStatus.IN_PROGRESS).count(),
        "resolved": qs.filter(status=TicketStatus.RESOLVED).count(),
        "high_open": qs.filter(severity="high")
        .exclude(status=TicketStatus.RESOLVED)
        .count(),
        "unassigned_open": qs.filter(
            status__in=_OPEN_TICKET_STATUSES, assigned_to__isnull=True
        ).count(),
    }


def _resolved_by_token(agent):
    """The ``resolved_by`` token this agent's resolutions are stamped with
    (mirrors views_tickets: ``agent:<code>`` else the name)."""
    if agent is None:
        return ""
    if agent.agent_code:
        return f"agent:{agent.agent_code}"
    return agent.name or ""


def _agent_name_map(tokens):
    """Map ``resolved_by`` tokens to display names. ``agent:<code>`` resolves to
    the Agent's name; ``user:<x>`` / plain strings display as-is."""
    codes = {
        t.split("agent:", 1)[1]
        for t in tokens
        if t and t.startswith("agent:") and t.split("agent:", 1)[1]
    }
    by_code = {}
    if codes:
        by_code = {
            a.agent_code: a.name
            for a in Agent.objects.filter(agent_code__in=codes)
        }

    def label(token):
        if not token:
            return "Unknown"
        if token.startswith("agent:"):
            code = token.split("agent:", 1)[1]
            return by_code.get(code, f"Agent {code}")
        if token.startswith("user:"):
            return token.split("user:", 1)[1]
        return token

    return label


# ── views ───────────────────────────────────────────────────────────────────
class CSDashboardSummaryView(PortalAPIView):
    """GET /portal/cs-dashboard/ — the CS command-center summary."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_cs(agent):
            return Response(
                {"detail": "Customer Service access required."}, status=403
            )

        now = timezone.now()
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())

        # Personal slice for the calling agent.
        my_open = 0
        my_resolved_today = 0
        my_resolved_week = 0
        if agent is not None:
            my_open = Ticket.objects.filter(
                assigned_to=agent, status__in=_OPEN_TICKET_STATUSES
            ).count()
            token = _resolved_by_token(agent)
            if token:
                resolved_mine = Ticket.objects.filter(
                    resolved_by=token, status=TicketStatus.RESOLVED
                )
                my_resolved_today = resolved_mine.filter(
                    resolved_at__date=today
                ).count()
                my_resolved_week = resolved_mine.filter(
                    resolved_at__date__gte=week_start
                ).count()

        return Response({
            "triage": _care_management_summary(now),
            "coverage": _coverage_counts(),
            "verification": _verification_summary(now),
            "tickets": _ticket_counts(),
            "me": {
                "agent_name": agent.name if agent else "",
                "open_assigned": my_open,
                "resolved_today": my_resolved_today,
                "resolved_week": my_resolved_week,
            },
        })


class CSDashboardTrendsView(PortalAPIView):
    """GET /portal/cs-dashboard/trends/?days=30 — daily opened/resolved series."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_cs(agent):
            return Response(
                {"detail": "Customer Service access required."}, status=403
            )

        try:
            days = min(180, max(7, int(request.query_params.get("days") or 30)))
        except (TypeError, ValueError):
            days = 30
        today = timezone.localdate()
        start = today - timedelta(days=days - 1)

        def daily(qs, field):
            counts = {
                r["day"]: r["n"]
                for r in qs.annotate(day=TruncDate(field))
                .values("day")
                .annotate(n=Count("id"))
                if r["day"] is not None
            }
            return counts

        tickets_opened = daily(
            Ticket.objects.filter(created_at__date__gte=start), "created_at"
        )
        tickets_resolved = daily(
            Ticket.objects.filter(
                status=TicketStatus.RESOLVED, resolved_at__date__gte=start
            ),
            "resolved_at",
        )
        warnings_opened = daily(
            MemberWarning.objects.filter(first_detected_at__date__gte=start),
            "first_detected_at",
        )
        warnings_resolved = daily(
            MemberWarning.objects.filter(
                status=WarningStatus.RESOLVED, resolved_at__date__gte=start
            ),
            "resolved_at",
        )

        series = []
        for i in range(days):
            d = start + timedelta(days=i)
            series.append({
                "date": d.isoformat(),
                "tickets_opened": tickets_opened.get(d, 0),
                "tickets_resolved": tickets_resolved.get(d, 0),
                "warnings_opened": warnings_opened.get(d, 0),
                "warnings_resolved": warnings_resolved.get(d, 0),
            })

        # Ticket resolution-time summary over the window.
        durations = [
            (r["resolved_at"] - r["created_at"]).total_seconds() / 3600.0
            for r in Ticket.objects.filter(
                status=TicketStatus.RESOLVED, resolved_at__date__gte=start
            ).values("created_at", "resolved_at")
            if r["resolved_at"] and r["created_at"] and r["resolved_at"] >= r["created_at"]
        ]
        resolution = {
            "count": len(durations),
            "avg_hours": round(statistics.mean(durations), 1) if durations else 0,
            "median_hours": round(statistics.median(durations), 1) if durations else 0,
        }

        return Response({"days": days, "series": series, "resolution": resolution})


class CSTicketManagerStatsView(PortalAPIView):
    """GET /portal/cs-dashboard/ticket-stats/?from=&to= — manager ticket analytics."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_manager(agent):
            return Response({"detail": "Management access required."}, status=403)

        now = timezone.now()
        today = timezone.localdate()
        date_from = _parse_date(request.query_params.get("from")) or (
            today - timedelta(days=29)
        )
        date_to = _parse_date(request.query_params.get("to")) or today

        all_tickets = Ticket.objects.all()
        open_tickets = all_tickets.filter(status__in=_OPEN_TICKET_STATUSES)
        resolved_in_range = all_tickets.filter(
            status=TicketStatus.RESOLVED,
            resolved_at__date__gte=date_from,
            resolved_at__date__lte=date_to,
        )
        opened_in_range = all_tickets.filter(
            created_at__date__gte=date_from, created_at__date__lte=date_to
        )

        backlog = {
            "open": all_tickets.filter(status=TicketStatus.OPEN).count(),
            "in_progress": all_tickets.filter(
                status=TicketStatus.IN_PROGRESS
            ).count(),
            "high_open": open_tickets.filter(severity="high").count(),
            "unassigned_open": open_tickets.filter(assigned_to__isnull=True).count(),
            "opened_in_range": opened_in_range.count(),
            "resolved_in_range": resolved_in_range.count(),
        }

        # Breakdown by type (open backlog + resolved-in-range), labelled.
        type_labels = {
            t.code: t.label for t in TicketType.objects.all()
        }
        open_by_type = {
            r["type__code"]: r["n"]
            for r in open_tickets.values("type__code").annotate(n=Count("id"))
        }
        resolved_by_type = {
            r["type__code"]: r["n"]
            for r in resolved_in_range.values("type__code").annotate(n=Count("id"))
        }
        codes = set(open_by_type) | set(resolved_by_type)
        by_type = sorted(
            (
                {
                    "code": c,
                    "label": type_labels.get(c, c or "Unknown"),
                    "open": open_by_type.get(c, 0),
                    "resolved": resolved_by_type.get(c, 0),
                }
                for c in codes
            ),
            key=lambda x: (-x["open"], -x["resolved"]),
        )

        def breakdown(field, qs):
            return {
                (r[field] or "unknown"): r["n"]
                for r in qs.values(field).annotate(n=Count("id"))
            }

        by_severity = breakdown("severity", open_tickets)
        by_source = breakdown("source", open_tickets)
        by_origin = breakdown("origin", open_tickets)

        # Aging buckets for open tickets (by created_at).
        aging = _empty_buckets()
        for created_at in open_tickets.values_list("created_at", flat=True):
            aging[_bucket_age(_age_days(created_at, now))] += 1

        # Currently-assigned open workload per agent.
        assigned_open = [
            {
                "agent_id": str(r["assigned_to"]),
                "name": r["assigned_to__name"] or "Unknown",
                "open": r["n"],
            }
            for r in open_tickets.filter(assigned_to__isnull=False)
            .values("assigned_to", "assigned_to__name")
            .annotate(n=Count("id"))
            .order_by("-n")
        ]

        # Solved-by-agent over the window: count + avg resolution time, keyed on
        # the ``resolved_by`` stamp.
        rows = list(
            resolved_in_range.values("resolved_by", "created_at", "resolved_at")
        )
        label_for = _agent_name_map({r["resolved_by"] for r in rows})
        agg = {}
        for r in rows:
            token = r["resolved_by"] or ""
            bucket = agg.setdefault(token, {"count": 0, "hours": []})
            bucket["count"] += 1
            if (
                r["resolved_at"]
                and r["created_at"]
                and r["resolved_at"] >= r["created_at"]
            ):
                bucket["hours"].append(
                    (r["resolved_at"] - r["created_at"]).total_seconds() / 3600.0
                )
        solved_by_agent = sorted(
            (
                {
                    "name": label_for(token),
                    "resolved": data["count"],
                    "avg_hours": round(statistics.mean(data["hours"]), 1)
                    if data["hours"]
                    else None,
                }
                for token, data in agg.items()
            ),
            key=lambda x: -x["resolved"],
        )

        return Response({
            "range": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "backlog": backlog,
            "aging": aging,
            "by_type": by_type,
            "by_severity": by_severity,
            "by_source": by_source,
            "by_origin": by_origin,
            "assigned_open": assigned_open,
            "solved_by_agent": solved_by_agent,
        })

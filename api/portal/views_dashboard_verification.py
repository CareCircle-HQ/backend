"""Verification dashboard analytics (management / Verifiers / CS).

A single aggregate endpoint reporting on the household verification pipeline
(:class:`EnrollmentVerification`), plus a drill-down list endpoint. Sections:

* funnel      -- COHORT scoped by request date: of the verification requests
  raised in range, how many are now Verified / Kitchen Assignment / Service
  Active (conversion + drop-off).
* queue       -- SNAPSHOT (ignores range): open pending count, aging buckets by
  days-since-request, and time-to-verify (avg/median) for completions in range.
* throughput  -- EVENT scoped by range: requests raised vs verifications
  completed, net backlog change, and % completed within the SLA target.
* agents      -- EVENT scoped by range: top verifiers (verified_by) and top
  requesters (requested_by).
* quality     -- SNAPSHOT over the verified population: Step-4 completeness,
  "verified but stuck" on authorization, and missing-data flags.

The request date used for scoping is COALESCE(requested_at, opened_at): a
bulk-imported enrollment with no explicit request stamp falls back to its row
creation time.
"""

import statistics
from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    ServiceAuthorizationStatus,
)
from .base import PortalAPIView, current_agent
from .views_dashboard import resolve_window

# Stages that mean the household reached (or passed) active meal service.
_KITCHEN_OR_BEYOND = [
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
]
_SERVICE_OR_BEYOND = [
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
]

# Verification requests older than this (days since request) breach the SLA.
_SLA_TARGET_DAYS = 7

# Drill-down reasons the verification list endpoint understands.
VERIFICATION_REASONS = frozenset({
    "pending",
    "aging_0_2", "aging_3_7", "aging_8_14", "aging_15_plus",
    "stuck_pending", "stuck_denied", "stuck_expired", "stuck_no_case",
    "step4_incomplete", "missing_address", "missing_case",
})

# All-three Step-4 validation checks completed.
_STEP4_ALL = (
    Q(is_family_verified=True)
    & Q(medicaid_type_verified=True)
    & Q(delivery_address_verified=True)
)


def _is_privileged(agent):
    """Verification dashboard is open to Management, Verifiers and CS (CS work
    verification alongside their own queue), plus any manager override."""
    if not agent:
        return False
    return (
        agent.group in ("Management", "Verifiers", "CS")
        or getattr(agent, "is_manager", False)
    )


def _with_req_date(qs):
    """Annotate each enrollment with ``req`` = COALESCE(requested_at, opened_at),
    the timestamp used as the verification-request date."""
    return qs.annotate(req=Coalesce("requested_at", "opened_at"))


def _scope_requests(qs, start, end):
    """Restrict enrollments to those REQUESTED within [start, end] (by request
    date). No-op for all-time (start is None)."""
    qs = _with_req_date(qs)
    if start is None:
        return qs
    return qs.filter(req__date__gte=start, req__date__lte=end)


def _base_ev():
    return EnrollmentVerification.objects.select_related(
        "client", "case", "delivery_address", "kitchen"
    )


def verification_enrollments(reason, start=None, end=None):
    """Return the EnrollmentVerification queryset behind one drill-down
    ``reason``. The dashboard counts are derived from the SAME querysets, so a
    count can never disagree with its list.

    The VERIFIED-population reasons (``stuck_*``, ``missing_*``,
    ``step4_incomplete``) honor the dashboard's date window on ``verified_at``
    (all-time when ``start`` is None). The pending-queue reasons (``pending`` /
    ``aging_*``) are a live snapshot -- 'right now' operational lists."""
    ev = _base_ev()
    today = timezone.localdate()

    def _in_window(qs):
        if start is None:
            return qs
        return qs.filter(verified_at__date__gte=start, verified_at__date__lte=end)

    if reason == "pending" or reason.startswith("aging_"):
        pending = _with_req_date(
            ev.filter(
                stage=EnrollmentStage.PENDING_VERIFICATION, verified_at__isnull=True
            )
        )
        if reason == "pending":
            return pending
        if reason == "aging_0_2":
            return pending.filter(req__date__gte=today - timedelta(days=2))
        if reason == "aging_3_7":
            return pending.filter(
                req__date__gte=today - timedelta(days=7),
                req__date__lte=today - timedelta(days=3),
            )
        if reason == "aging_8_14":
            return pending.filter(
                req__date__gte=today - timedelta(days=14),
                req__date__lte=today - timedelta(days=8),
            )
        if reason == "aging_15_plus":
            return pending.filter(req__date__lte=today - timedelta(days=15))

    if reason.startswith("stuck_"):
        stuck = _in_window(ev.filter(
            stage=EnrollmentStage.VERIFIED, verified_at__isnull=False
        ))
        if reason == "stuck_pending":
            return stuck.filter(
                case__service_authorization_status=ServiceAuthorizationStatus.PENDING
            )
        if reason == "stuck_denied":
            return stuck.filter(
                case__service_authorization_status=ServiceAuthorizationStatus.DENIED
            )
        if reason == "stuck_expired":
            return stuck.filter(
                case__service_authorization_status=ServiceAuthorizationStatus.EXPIRED
            )
        if reason == "stuck_no_case":
            return stuck.filter(case__isnull=True)

    verified = _in_window(ev.filter(verified_at__isnull=False))
    if reason == "step4_incomplete":
        return verified.exclude(_STEP4_ALL)
    if reason == "missing_address":
        return verified.filter(delivery_address__isnull=True)
    if reason == "missing_case":
        return verified.filter(case__isnull=True)

    return ev.none()


def _row_detail(reason, ev, today):
    """Human-readable per-row detail for the drill-down list."""
    if reason == "pending" or reason.startswith("aging_"):
        req = getattr(ev, "req", None) or ev.opened_at
        days = (today - timezone.localtime(req).date()).days if req else 0
        return f"Waiting {days} day{'s' if days != 1 else ''}"
    if reason.startswith("stuck_"):
        status = ev.case.service_authorization_status if ev.case_id else None
        label = {
            ServiceAuthorizationStatus.PENDING: "Authorization pending",
            ServiceAuthorizationStatus.DENIED: "Authorization denied",
            ServiceAuthorizationStatus.EXPIRED: "Authorization expired",
        }.get(status, "No governing case")
        return label
    if reason == "step4_incomplete":
        missing = []
        if not ev.is_family_verified:
            missing.append("family")
        if not ev.medicaid_type_verified:
            missing.append("Medicaid type")
        if not ev.delivery_address_verified:
            missing.append("address")
        return "Unchecked: " + ", ".join(missing) if missing else "Incomplete checks"
    if reason == "missing_address":
        return "No delivery address on file"
    if reason == "missing_case":
        return "No linked internal-service case"
    return ""


class VerificationDashboardView(PortalAPIView):
    """Aggregate verification-pipeline analytics. See module docstring."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Verification dashboard access required."}, status=403
            )

        period = (request.query_params.get("period") or "all").lower()
        # Accept an explicit custom range (?start=&end=, ISO YYYY-MM-DD) -- wins
        # over the named period preset -- so the dashboard's From/To pickers work.
        start, end = resolve_window(request)
        today = timezone.localdate()

        ev = EnrollmentVerification.objects

        # --- Funnel (COHORT: requests raised in range) --------------------
        cohort = _scope_requests(ev.all(), start, end)
        requested = cohort.count()
        verified = cohort.filter(verified_at__isnull=False).count()
        kitchen = cohort.filter(stage__in=_KITCHEN_OR_BEYOND).count()
        active = cohort.filter(stage__in=_SERVICE_OR_BEYOND).count()

        steps = [
            ("requested", "Requested", requested),
            ("verified", "Verified", verified),
            ("kitchen", "Kitchen Assignment", kitchen),
            ("service", "Service Active", active),
        ]
        first = steps[0][2]
        funnel = []
        prev = None
        for key, label, count in steps:
            funnel.append({
                "key": key,
                "label": label,
                "count": count,
                "pct_of_first": round(count / first * 100, 1) if first else 0.0,
                "pct_of_prev": (
                    round(count / prev * 100, 1) if prev else None
                ),
            })
            prev = count

        drop_off = {
            "disregarded": cohort.filter(stage=EnrollmentStage.DISREGARDED).count(),
            "cancelled": cohort.filter(stage=EnrollmentStage.CANCELLED).count(),
        }

        # --- Queue health (SNAPSHOT) --------------------------------------
        aging = {
            "d0_2": verification_enrollments("aging_0_2").count(),
            "d3_7": verification_enrollments("aging_3_7").count(),
            "d8_14": verification_enrollments("aging_8_14").count(),
            "d15_plus": verification_enrollments("aging_15_plus").count(),
        }
        open_pending = verification_enrollments("pending").count()

        # Time-to-verify over completions IN RANGE (by verified_at).
        completed_qs = ev.filter(verified_at__isnull=False)
        if start is not None:
            completed_qs = completed_qs.filter(
                verified_at__date__gte=start, verified_at__date__lte=end
            )
        dur_rows = _with_req_date(completed_qs).values_list("verified_at", "req")
        durations = [
            (v - r).total_seconds() / 86400.0
            for v, r in dur_rows
            if v and r and v >= r
        ]
        time_to_verify = {
            "count": len(durations),
            "avg_days": round(statistics.mean(durations), 1) if durations else 0.0,
            "median_days": (
                round(statistics.median(durations), 1) if durations else 0.0
            ),
        }

        # --- Throughput (EVENT scoped by range) ---------------------------
        completed = completed_qs.count()
        within = sum(1 for d in durations if d <= _SLA_TARGET_DAYS)
        throughput = {
            "requests_raised": requested,
            "completed": completed,
            "net_backlog": requested - completed,
            "sla_target_days": _SLA_TARGET_DAYS,
            "sla_within": within,
            "sla_pct": (
                round(within / len(durations) * 100, 1) if durations else 0.0
            ),
        }

        # --- Agents (EVENT scoped by range) -------------------------------
        # Accurate per-agent count mirrors the "All Verifications" report / the
        # Verification page: ONE verification per household (else per solo client)
        # -- the most-recent verified enrollment -- excluding dismissed
        # (Disregarded) + parked (Scheduled Extension) rows whose verified_at is a
        # stale prior-cycle fact. Counting raw enrollment rows over-credits an
        # agent when a household holds several verified enrollments (a superseded /
        # carried row from a governing-case switch, or a split-out dependent), so
        # dedupe by household before tallying by verified_by.
        _nongov = [EnrollmentStage.DISREGARDED, EnrollmentStage.SCHEDULED_EXTENSION]
        seen_hh = set()
        v_tally = {}  # verified_by id -> [name, count]
        for hh_id, cli_id, vby, vname in (
            completed_qs.exclude(stage__in=_nongov)
            .order_by("-verified_at", "-opened_at")
            .values_list(
                "household_id", "client_id", "verified_by", "verified_by__name"
            )
            .iterator(chunk_size=2000)
        ):
            key = ("hh", hh_id) if hh_id else ("c", cli_id)
            if key in seen_hh:
                continue
            seen_hh.add(key)
            if vby is None:
                continue  # verified with no attributable agent -> don't credit
            t = v_tally.get(vby)
            if t is None:
                t = v_tally[vby] = [vname or "Unknown", 0]
            t[1] += 1
        verifiers = sorted(
            (
                {"id": str(vby), "name": t[0], "count": t[1]}
                for vby, t in v_tally.items()
            ),
            key=lambda r: r["count"],
            reverse=True,
        )[:8]
        requesters = [
            {
                "id": str(r["requested_by"]),
                "name": r["requested_by__name"] or "Unknown",
                "count": r["n"],
            }
            for r in (
                cohort.filter(requested_by__isnull=False)
                .values("requested_by", "requested_by__name")
                .annotate(n=Count("id"))
                .order_by("-n")[:8]
            )
        ]

        # --- Quality & bottlenecks (over verifications COMPLETED in range) ----
        # Scoped to the selected date window via ``completed_qs`` (verified_at in
        # range; all-time when no range) so the Verified-but-Stuck + Missing-Data
        # cards move with the picker -- and match their drill-down lists, which
        # apply the SAME window (see verification_enrollments).
        verified_all = completed_qs
        verified_total = verified_all.count()

        stuck = verified_all.filter(stage=EnrollmentStage.VERIFIED)
        stuck_total = stuck.count()
        stuck_pending = stuck.filter(
            case__service_authorization_status=ServiceAuthorizationStatus.PENDING
        ).count()
        stuck_denied = stuck.filter(
            case__service_authorization_status=ServiceAuthorizationStatus.DENIED
        ).count()
        stuck_expired = stuck.filter(
            case__service_authorization_status=ServiceAuthorizationStatus.EXPIRED
        ).count()
        stuck_no_case = stuck.filter(case__isnull=True).count()
        stuck_payload = {
            "total": stuck_total,
            "pending": stuck_pending,
            "denied": stuck_denied,
            "expired": stuck_expired,
            "no_case": stuck_no_case,
            # Verified + not advanced yet the auth is Approved/blank -- an
            # anomaly (should have advanced) worth flagging for review.
            "other": (
                stuck_total - stuck_pending - stuck_denied
                - stuck_expired - stuck_no_case
            ),
        }

        missing = {
            "address": verified_all.filter(delivery_address__isnull=True).count(),
            "case": verified_all.filter(case__isnull=True).count(),
        }

        return Response({
            "period": period,
            "range": (
                {"start": start.isoformat(), "end": end.isoformat()}
                if start is not None else None
            ),
            "funnel": funnel,
            "drop_off": drop_off,
            "queue": {
                "open_pending": open_pending,
                "aging": aging,
                "time_to_verify": time_to_verify,
            },
            "throughput": throughput,
            "agents": {"verifiers": verifiers, "requesters": requesters},
            "quality": {
                "verified_total": verified_total,
                "stuck": stuck_payload,
                "missing": missing,
            },
        })


class VerificationDashboardListView(PortalAPIView):
    """Drill-down: the individual enrollments behind one verification ``reason``
    (see :data:`VERIFICATION_REASONS`). Each row names the household primary /
    case holder and links to their member profile."""

    def get(self, request, reason):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Verification dashboard access required."}, status=403
            )
        if reason not in VERIFICATION_REASONS:
            return Response({"detail": "Unknown reason."}, status=404)

        today = timezone.localdate()
        # Honor the dashboard's date window for the verified-population reasons
        # (stuck_* / missing_* / step4_incomplete) so a card's count matches its
        # expanded list. Pending/aging reasons ignore it (live snapshot).
        start, end = resolve_window(request)
        qs = verification_enrollments(reason, start, end).order_by("req" if (
            reason == "pending" or reason.startswith("aging_")
        ) else "-verified_at")[:200]

        # Names for enrollments whose client row didn't come back via
        # select_related (defensive; client is normally joined).
        results = []
        for e in qs:
            client = e.client
            cid = str(e.client_id) if e.client_id else str(e.pk)
            name = (
                f"{client.first_name} {client.last_name}".strip()
                if client else cid
            ) or cid
            results.append({
                "id": cid,
                "name": name,
                "code": e.code or "",
                "stage": e.stage,
                "detail": _row_detail(reason, e, today),
            })

        return Response({
            "reason": reason,
            "count": len(results),
            "results": results,
        })

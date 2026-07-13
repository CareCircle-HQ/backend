"""Logistics dashboard analytics (Logistics / Management).

A single aggregate endpoint reporting on the fulfillment pipeline, plus a
drill-down list endpoint. Sections:

* queue      -- SNAPSHOT: households awaiting kitchen assignment, using the
  EXACT definition the Logistics page renders (``MembersListView`` scope=logistics
  + the renderable-group drop) so the count reconciles with that page; aging by
  ``stage_at``, at-risk/unassignable households (no active kitchen can serve every
  member), and PO-blocker stats (see :func:`po_blocker_stats`).
* capacity   -- SNAPSHOT + FORECAST: per-kitchen active load, kitchens by
  status, and over-capacity delivery days in the next week.
* forecast   -- FORECAST (next 7 / 14 days, ignores the date selector):
  scheduled deliveries + meals per day, menu-type mix, and per-kitchen load.
* kitchen_orders -- RANGE (by PurchaseOrder.delivery_date): POs by status +
  volume per kitchen, the kitchen fulfillment funnel (kitchen_status), rerouted
  deliveries, and time-to-send.
* outcomes   -- RANGE (by DeliveryOrder.expected_delivery_date): delivery status
  funnel, failed/returned + on-time rates, delivered volume; plus Out of Orbit /
  Out of Range fulfillment exclusions (snapshot).

Assignment unit is the household (enrollment). Volume is meals/boxes via
``DeliveryOrder.quantity`` / ``OrderSchedule.how_many_meals_or_boxes``.
"""

from datetime import timedelta

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Sum
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from ..models import (
    Client,
    DeliveryOrder,
    DeliveryOrderStatus,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MemberStatus,
    OrderSchedule,
    OrderStatus,
    ProductTypeKind,
    PurchaseOrder,
    PurchaseOrderKitchenStatus,
    PurchaseOrderStatus,
)
from .serializers import active_enrollment
from ..services.catalog import product_type_kind_for_name
from ..services.po_blockers import (
    BLOCKED_REASONS,
    FIXABLE_REASONS,
    REASON_DESCRIPTIONS,
    REASON_LABELS,
    classify_po_blockers,
    summarize_po_blockers,
)
from .base import PortalAPIView, current_agent
from .views_dashboard import period_window
from .views_members import MembersListView

# A PO not yet sent whose delivery is within this many days is "at risk".
_IMMINENT_DAYS = 2

# Drill-down reasons the logistics list endpoint understands.
LOGISTICS_REASONS = frozenset({
    "awaiting", "aging_0_2", "aging_3_7", "aging_8_14", "aging_15_plus",
    "at_risk", "out_of_orbit", "out_of_range",
})


def _is_privileged(agent):
    """Logistics dashboard: Logistics + Management groups (+ manager override)."""
    if not agent:
        return False
    return (
        agent.group in ("Logistics", "Management")
        or getattr(agent, "is_manager", False)
    )


def _age_bucket(stage_at, today):
    """Aging bucket key for a household that entered the queue on ``stage_at``.
    Missing timestamps count as freshly-queued (d0_2)."""
    if stage_at is None:
        return "d0_2"
    days = (today - timezone.localtime(stage_at).date()).days
    if days <= 2:
        return "d0_2"
    if days <= 7:
        return "d3_7"
    if days <= 14:
        return "d8_14"
    return "d15_plus"


def queue_group_rows():
    """The households/individuals awaiting kitchen assignment, using the EXACT
    definition the Logistics page renders (``MembersListView`` scope=logistics +
    the renderable-group drop that hides households whose every member is
    out-of-orbit or has a finished internal-service case). This is why the count
    reconciles with the Logistics page instead of the raw enrollment stage count.

    Each row: ``{key, client_id, name, stage_at, kitchen_available, blockers}``
    where ``key`` is the ``(type, id)`` group key and ``client_id`` is the
    household primary (for the member drill-down link)."""
    view = MembersListView()
    view.request = Request(
        APIRequestFactory().get("/portal/members/", {"scope": "logistics"})
    )
    view.kwargs = {}
    entries = view._group_entries()
    keys = view._renderable_keys(entries)
    entries = [e for e in entries if (e["type"], e["id"]) in keys]
    if not entries:
        return []
    # Readiness checkers (per group) -- reuses the page's serviceability calc, so
    # "kitchen_available" here is identical to the page's "Kitchen needs review".
    checks = view._compute_logistics_checks(entries)

    hh_ids = [e["id"] for e in entries if e["type"] == "household"]
    ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
    primary_by_hh = {}
    if hh_ids:
        for hm in HouseholdMember.objects.filter(
            household_id__in=hh_ids, is_primary=True
        ).select_related("client"):
            primary_by_hh[hm.household_id] = hm.client
    ind_clients = (
        {c.client_id: c for c in Client.objects.filter(client_id__in=ind_ids)}
        if ind_ids else {}
    )

    rows = []
    for e in entries:
        key = (e["type"], e["id"])
        client = (
            primary_by_hh.get(e["id"]) if e["type"] == "household"
            else ind_clients.get(e["id"])
        )
        if client is None:
            continue
        enr = active_enrollment(client)
        agg = checks.get(key, (None, {}))[1] or {}
        rows.append({
            "key": key,
            "client_id": str(client.client_id),
            "name": e["name"] or f"{client.first_name} {client.last_name}".strip(),
            "stage_at": getattr(enr, "stage_at", None),
            "kitchen_available": agg.get("kitchen_available", True),
            "blockers": agg.get("blockers", []),
        })
    return rows


def logistics_queue_rows(reason):
    """Filter :func:`queue_group_rows` to the queue-side drill-down ``reason``."""
    today = timezone.localdate()
    rows = queue_group_rows()
    if reason == "awaiting":
        return rows
    if reason == "at_risk":
        return [r for r in rows if not r["kitchen_available"]]
    if reason in ("aging_0_2", "aging_3_7", "aging_8_14", "aging_15_plus"):
        want = reason.replace("aging_", "d")
        return [r for r in rows if _age_bucket(r["stage_at"], today) == want]
    return []


def po_blocker_stats():
    """Per-reason PO-blocker counts + metadata (mirrors POBlockersStatsView) for
    the At-Risk / Unassignable section: active members with a live delivery plan
    that can't reach a Purchase Order."""
    rows = classify_po_blockers(include_ok=False)
    counts = summarize_po_blockers(rows)
    reasons = [
        {
            "reason": r,
            "label": REASON_LABELS.get(r, r),
            "description": REASON_DESCRIPTIONS.get(r, ""),
            "count": counts.get(r, 0),
            "fixable": r in FIXABLE_REASONS,
        }
        for r in BLOCKED_REASONS
        if counts.get(r, 0) > 0
    ]
    reasons.sort(key=lambda x: -x["count"])
    return {"total": len(rows), "reasons": reasons}


class LogisticsDashboardView(PortalAPIView):
    """Aggregate fulfillment-pipeline analytics. See module docstring."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )

        period = (request.query_params.get("period") or "all").lower()
        start, end = period_window(period)
        today = timezone.localdate()

        return Response({
            "period": period,
            "range": (
                {"start": start.isoformat(), "end": end.isoformat()}
                if start is not None else None
            ),
            "queue": self._queue(today),
            "capacity": self._capacity(today),
            "forecast": self._forecast(today),
            "kitchen_orders": self._kitchen_orders(start, end),
            "outcomes": self._outcomes(start, end),
        })

    # -- Section 1: assignment queue (snapshot) ------------------------------
    def _queue(self, today):
        rows = queue_group_rows()
        aging = {"d0_2": 0, "d3_7": 0, "d8_14": 0, "d15_plus": 0}
        at_risk = 0
        for r in rows:
            aging[_age_bucket(r["stage_at"], today)] += 1
            if not r["kitchen_available"]:
                at_risk += 1
        return {
            "awaiting": len(rows),
            "aging": aging,
            "at_risk": at_risk,
            "po_blockers": po_blocker_stats(),
        }

    # -- Section 2: kitchen capacity & load (snapshot + forecast) ------------
    def _capacity(self, today):
        window_end = today + timedelta(days=6)
        # Orders due per (kitchen, date) in the next 7 days -> over-capacity days.
        due = (
            OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                anticipated_delivery_date__lte=window_end,
            )
            .exclude(kitchen__isnull=True)
            .values("kitchen_id", "anticipated_delivery_date")
            .annotate(n=Count("order_id"))
        )
        due_by_kitchen = {}
        for row in due:
            due_by_kitchen.setdefault(str(row["kitchen_id"]), []).append(row["n"])

        # Current active load (households in service) per kitchen.
        load = {
            str(row["kitchen_id"]): row["n"]
            for row in EnrollmentVerification.objects.filter(
                stage=EnrollmentStage.SERVICE_ACTIVE, kitchen__isnull=False
            )
            .values("kitchen_id")
            .annotate(n=Count("id"))
        }

        kitchens = []
        status_counts = {"active": 0, "inactive": 0, "suspended": 0}
        meals_kitchens = boxes_kitchens = 0
        for k in Kitchen.objects.all().order_by("name"):
            status_counts[k.status] = status_counts.get(k.status, 0) + 1
            products = k.supported_products or []
            if "meal" in products:
                meals_kitchens += 1
            if "box" in products:
                boxes_kitchens += 1
            cap = k.max_orders_per_day
            per_day = due_by_kitchen.get(str(k.pk), [])
            over_days = sum(1 for n in per_day if cap is not None and n > cap)
            kitchens.append({
                "id": str(k.pk),
                "name": k.name,
                "status": k.status,
                "capacity": cap,
                "active_load": load.get(str(k.pk), 0),
                "supported_products": products,
                "over_capacity_days": over_days,
                "peak_due": max(per_day) if per_day else 0,
            })
        # Busiest / most-loaded kitchens first.
        kitchens.sort(key=lambda r: (-r["active_load"], r["name"].lower()))
        return {
            "kitchens": kitchens,
            "status_counts": status_counts,
            "product_coverage": {"meals": meals_kitchens, "boxes": boxes_kitchens},
        }

    # -- Section 3: production forecast (fixed forward window) ---------------
    def _forecast(self, today):
        end14 = today + timedelta(days=13)
        # Per-day totals split Meals/Boxes. Group by (date, program_name) so we
        # classify the kind on a small number of rows.
        rows = (
            OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                anticipated_delivery_date__lte=end14,
            )
            .values("anticipated_delivery_date", "program_name")
            .annotate(deliveries=Count("order_id"), qty=Sum("how_many_meals_or_boxes"))
        )
        kind_cache = {}

        def kind_of(name):
            if name not in kind_cache:
                kind_cache[name] = product_type_kind_for_name(name)
            return kind_cache[name]

        by_date = {}
        for r in rows:
            d = r["anticipated_delivery_date"].isoformat()
            bucket = by_date.setdefault(
                d, {"date": d, "meals": 0, "boxes": 0, "deliveries": 0}
            )
            qty = r["qty"] or 0
            bucket["deliveries"] += r["deliveries"]
            if kind_of(r["program_name"]) == ProductTypeKind.BOXES:
                bucket["boxes"] += qty
            else:
                bucket["meals"] += qty
        days = [by_date.get((today + timedelta(days=i)).isoformat(),
                             {"date": (today + timedelta(days=i)).isoformat(),
                              "meals": 0, "boxes": 0, "deliveries": 0})
                for i in range(14)]

        def totals(day_slice):
            return {
                "meals": sum(d["meals"] for d in day_slice),
                "boxes": sum(d["boxes"] for d in day_slice),
                "deliveries": sum(d["deliveries"] for d in day_slice),
            }

        end7 = today + timedelta(days=6)
        # Menu-type mix + per-kitchen load over the next 7 days.
        menu_mix = [
            {"code": r["menu_type"] or "—", "meals": r["qty"] or 0, "deliveries": r["n"]}
            for r in OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                anticipated_delivery_date__lte=end7,
            )
            .values("menu_type")
            .annotate(qty=Sum("how_many_meals_or_boxes"), n=Count("order_id"))
            .order_by("-n")
        ]
        by_kitchen = [
            {
                "id": str(r["kitchen_id"]) if r["kitchen_id"] else None,
                "name": r["kitchen__name"] or "Unassigned",
                "deliveries": r["n"],
                "meals": r["qty"] or 0,
            }
            for r in OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                anticipated_delivery_date__lte=end7,
            )
            .values("kitchen_id", "kitchen__name")
            .annotate(n=Count("order_id"), qty=Sum("how_many_meals_or_boxes"))
            .order_by("-n")[:10]
        ]
        return {
            "days": days,
            "next_7": totals(days[:7]),
            "next_14": totals(days),
            "menu_mix": menu_mix,
            "by_kitchen": by_kitchen,
        }

    # -- Section 4: kitchen orders (range by delivery_date) ------------------
    def _kitchen_orders(self, start, end):
        po = PurchaseOrder.objects.all()
        do = DeliveryOrder.objects.all()
        if start is not None:
            po = po.filter(delivery_date__gte=start, delivery_date__lte=end)
            do = do.filter(
                expected_delivery_date__gte=start, expected_delivery_date__lte=end
            )

        po_status = {
            row["status"]: row["n"]
            for row in po.values("status").annotate(n=Count("purchase_order_id"))
        }
        kitchen_funnel = {
            row["kitchen_status"]: row["n"]
            for row in po.values("kitchen_status").annotate(n=Count("purchase_order_id"))
        }

        # Volume + PO count per kitchen (top by volume).
        vol_by_kitchen = {
            str(r["kitchen_id"]): (r["qty"] or 0)
            for r in do.exclude(kitchen__isnull=True)
            .values("kitchen_id")
            .annotate(qty=Sum("quantity"))
        }
        po_by_kitchen = {
            str(r["kitchen_id"]): r["n"]
            for r in po.exclude(kitchen__isnull=True)
            .values("kitchen_id")
            .annotate(n=Count("purchase_order_id"))
        }
        names = {
            str(k.pk): k.name
            for k in Kitchen.objects.filter(
                pk__in=set(vol_by_kitchen) | set(po_by_kitchen)
            )
        }
        per_kitchen = sorted(
            (
                {
                    "id": kid,
                    "name": names.get(kid, "—"),
                    "pos": po_by_kitchen.get(kid, 0),
                    "volume": vol_by_kitchen.get(kid, 0),
                }
                for kid in set(vol_by_kitchen) | set(po_by_kitchen)
            ),
            key=lambda r: -r["volume"],
        )[:10]

        # At-risk: not yet sent to the kitchen with an imminent delivery date.
        today = timezone.localdate()
        at_risk = po.filter(
            kitchen_status=PurchaseOrderKitchenStatus.NOT_SENT,
            delivery_date__isnull=False,
            delivery_date__lte=today + timedelta(days=_IMMINENT_DAYS),
        ).count()

        # Rerouted deliveries: actual kitchen differs from the default.
        rerouted = (
            do.exclude(kitchen__isnull=True)
            .exclude(default_kitchen__isnull=True)
            .exclude(kitchen=F("default_kitchen"))
            .count()
        )

        # Time-to-send (hours): sent_to_kitchen_at - created_at over sent POs.
        sent = po.filter(sent_to_kitchen_at__isnull=False).annotate(
            lead=ExpressionWrapper(
                F("sent_to_kitchen_at") - F("created_at"), output_field=DurationField()
            )
        )
        avg_lead = sent.aggregate(a=Avg("lead"))["a"]
        avg_send_hours = round(avg_lead.total_seconds() / 3600.0, 1) if avg_lead else 0.0

        return {
            "total_pos": po.count(),
            "total_volume": do.aggregate(q=Sum("quantity"))["q"] or 0,
            "po_status": {
                "draft": po_status.get(PurchaseOrderStatus.DRAFT, 0),
                "confirmed": po_status.get(PurchaseOrderStatus.CONFIRMED, 0),
                "completed": po_status.get(PurchaseOrderStatus.COMPLETED, 0),
                "cancelled": po_status.get(PurchaseOrderStatus.CANCELLED, 0),
            },
            "kitchen_funnel": {
                "not_sent": kitchen_funnel.get(PurchaseOrderKitchenStatus.NOT_SENT, 0),
                "sent": kitchen_funnel.get(PurchaseOrderKitchenStatus.SENT_TO_KITCHEN, 0),
                "accepted": kitchen_funnel.get(
                    PurchaseOrderKitchenStatus.ACCEPTED_BY_KITCHEN, 0
                ),
                "in_preparation": kitchen_funnel.get(
                    PurchaseOrderKitchenStatus.IN_PREPARATION, 0
                ),
                "ready_for_dispatch": kitchen_funnel.get(
                    PurchaseOrderKitchenStatus.READY_FOR_DISPATCH, 0
                ),
            },
            "per_kitchen": per_kitchen,
            "at_risk_not_sent": at_risk,
            "rerouted": rerouted,
            "avg_send_hours": avg_send_hours,
        }

    # -- Section 5: delivery outcomes & exclusions ---------------------------
    def _outcomes(self, start, end):
        do = DeliveryOrder.objects.all()
        if start is not None:
            do = do.filter(
                expected_delivery_date__gte=start, expected_delivery_date__lte=end
            )
        status_counts = {
            row["status"]: row["n"]
            for row in do.values("status").annotate(n=Count("delivery_order_id"))
        }
        delivered = status_counts.get(DeliveryOrderStatus.DELIVERED, 0)
        failed = status_counts.get(DeliveryOrderStatus.FAILED, 0)
        returned = status_counts.get(DeliveryOrderStatus.RETURNED, 0)
        total = sum(status_counts.values())

        # On-time: delivered on/before the expected date.
        on_time = do.filter(
            status=DeliveryOrderStatus.DELIVERED,
            delivered_at__isnull=False,
            delivered_at__date__lte=F("expected_delivery_date"),
        ).count()
        delivered_volume = (
            do.filter(status=DeliveryOrderStatus.DELIVERED).aggregate(
                q=Sum("quantity")
            )["q"]
            or 0
        )

        # Exclusions (snapshot, distinct members): dietary + geographic blocks.
        mdp = MemberDietaryProfile.objects
        out_of_orbit = (
            mdp.filter(status=MemberStatus.OUT_OF_ORBIT)
            .values("client_id").distinct().count()
        )
        out_of_range = (
            mdp.filter(status=MemberStatus.OUT_OF_RANGE)
            .values("client_id").distinct().count()
        )

        return {
            "total": total,
            "status": {
                "pending": status_counts.get(DeliveryOrderStatus.PENDING, 0),
                "ready": status_counts.get(DeliveryOrderStatus.READY_FOR_DELIVERY, 0),
                "out": status_counts.get(DeliveryOrderStatus.OUT_FOR_DELIVERY, 0),
                "delivered": delivered,
                "failed": failed,
                "returned": returned,
                "cancelled": status_counts.get(DeliveryOrderStatus.CANCELLED, 0),
                "on_hold": status_counts.get(DeliveryOrderStatus.ON_HOLD, 0),
            },
            "delivered": delivered,
            "delivered_volume": delivered_volume,
            "failed_returned": failed + returned,
            "failed_rate": (
                round((failed + returned) / total * 100, 1) if total else 0.0
            ),
            "on_time": on_time,
            "on_time_rate": round(on_time / delivered * 100, 1) if delivered else 0.0,
            "exclusions": {"out_of_orbit": out_of_orbit, "out_of_range": out_of_range},
        }


class LogisticsDashboardListView(PortalAPIView):
    """Drill-down: the members/households behind one logistics ``reason`` (see
    :data:`LOGISTICS_REASONS`). Each row links to the member profile."""

    def get(self, request, reason):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )
        if reason not in LOGISTICS_REASONS:
            return Response({"detail": "Unknown reason."}, status=404)

        today = timezone.localdate()

        if reason in ("out_of_orbit", "out_of_range"):
            status = (
                MemberStatus.OUT_OF_ORBIT if reason == "out_of_orbit"
                else MemberStatus.OUT_OF_RANGE
            )
            rows = (
                MemberDietaryProfile.objects.filter(status=status)
                .select_related("client")[:200]
            )
            results = []
            for m in rows:
                cid = str(m.client_id) if m.client_id else str(m.pk)
                results.append({
                    "id": cid,
                    "name": (m.member_name or cid),
                    "detail": (m.menu_type or "").strip() or "—",
                })
            results.sort(key=lambda r: r["name"].lower())
            return Response({"reason": reason, "count": len(results), "results": results})

        # Queue-side reasons (household-based; mirrors the Logistics page).
        rows = logistics_queue_rows(reason)
        rows.sort(key=lambda r: (r["stage_at"] is not None, r["stage_at"] or today))
        results = []
        for r in rows[:200]:
            days = (
                (today - timezone.localtime(r["stage_at"]).date()).days
                if r["stage_at"] else 0
            )
            detail = f"Waiting {days} day{'s' if days != 1 else ''}"
            if reason == "at_risk" and r["blockers"]:
                detail += " · " + ", ".join(r["blockers"])
            results.append({"id": r["client_id"], "name": r["name"], "detail": detail})
        return Response({"reason": reason, "count": len(results), "results": results})

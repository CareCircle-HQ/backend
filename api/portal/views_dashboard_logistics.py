"""Logistics dashboard analytics (Logistics / Management).

A single aggregate endpoint reporting on the fulfillment pipeline, plus a
drill-down list endpoint. Sections:

* queue      -- SNAPSHOT: households awaiting kitchen assignment (stage
  KITCHEN_ASSIGNMENT, open case, not On Hold), aging by ``stage_at``, and the
  at-risk/unassignable households (no active kitchen can serve every member).
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

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    Case,
    CaseStatus,
    Client,
    DeliveryOrder,
    DeliveryOrderStatus,
    EnrollmentStage,
    EnrollmentVerification,
    Kitchen,
    KitchenStatus,
    MemberDietaryProfile,
    MemberStatus,
    OrderSchedule,
    OrderStatus,
    ProductTypeKind,
    PurchaseOrder,
    PurchaseOrderKitchenStatus,
    PurchaseOrderStatus,
)
from ..services.catalog import product_kind_for_enrollment, product_type_kind_for_name
from ..services.kitchens import member_coverage_for_kitchen, required_product_for_program
from .base import PortalAPIView, current_agent
from .views_dashboard import period_window

_TERMINAL_CASE_STATUSES = [CaseStatus.CLOSED, CaseStatus.CANCELLED]

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


def _queue_base():
    """Households awaiting kitchen assignment: enrollments at KITCHEN_ASSIGNMENT
    whose governing internal-service case is still open (not Closed/Cancelled).
    On Hold is a different stage, so it is already excluded."""
    return (
        EnrollmentVerification.objects.filter(
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT
        )
        .filter(Q(case__isnull=True) | ~Q(case__case_status__in=_TERMINAL_CASE_STATUSES))
        .select_related("client", "case")
    )


def _active_kitchens():
    return list(
        Kitchen.objects.filter(status=KitchenStatus.ACTIVE).prefetch_related(
            "kitchen_menu_types__menu_type", "kitchen_menu_types__restrictions"
        )
    )


def _household_serviceable(enrollment, kitchens, members=None):
    """True when at least one of ``kitchens`` supports the enrollment's product
    kind AND covers every member (menu type + food allergies)."""
    members = members if members is not None else list(enrollment.member_profiles.all())
    if not members:
        return True  # nothing to serve yet -> not "at risk"
    required = required_product_for_program(enrollment.program_name)
    for k in kitchens:
        if required is not None and required not in (k.supported_products or []):
            continue
        if all(member_coverage_for_kitchen(m, k)[0] for m in members):
            return True
    return False


def _at_risk_enrollment_ids():
    """Queued households that NO active kitchen can fully serve (would go Out of
    Orbit at assignment). Returns a list of EnrollmentVerification pks."""
    kitchens = _active_kitchens()
    at_risk = []
    for enr in _queue_base().prefetch_related("member_profiles"):
        members = list(enr.member_profiles.all())
        if not _household_serviceable(enr, kitchens, members):
            at_risk.append(enr.pk)
    return at_risk


def logistics_enrollments(reason):
    """Return the queue-side drill-down queryset for ``reason`` (assignment
    queue + aging buckets + at-risk). Snapshot by design."""
    today = timezone.localdate()
    base = _queue_base()
    if reason == "awaiting":
        return base
    if reason == "aging_0_2":
        return base.filter(stage_at__date__gte=today - timedelta(days=2))
    if reason == "aging_3_7":
        return base.filter(
            stage_at__date__gte=today - timedelta(days=7),
            stage_at__date__lte=today - timedelta(days=3),
        )
    if reason == "aging_8_14":
        return base.filter(
            stage_at__date__gte=today - timedelta(days=14),
            stage_at__date__lte=today - timedelta(days=8),
        )
    if reason == "aging_15_plus":
        return base.filter(stage_at__date__lte=today - timedelta(days=15))
    if reason == "at_risk":
        return base.filter(pk__in=_at_risk_enrollment_ids())
    return EnrollmentVerification.objects.none()


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
        base = _queue_base()
        return {
            "awaiting": base.count(),
            "aging": {
                "d0_2": logistics_enrollments("aging_0_2").count(),
                "d3_7": logistics_enrollments("aging_3_7").count(),
                "d8_14": logistics_enrollments("aging_8_14").count(),
                "d15_plus": logistics_enrollments("aging_15_plus").count(),
            },
            "at_risk": len(_at_risk_enrollment_ids()),
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

        # Queue-side reasons (enrollment-based).
        qs = logistics_enrollments(reason).order_by("stage_at")[:200]
        results = []
        for e in qs:
            client = e.client
            cid = str(e.client_id) if e.client_id else str(e.pk)
            name = (
                f"{client.first_name} {client.last_name}".strip() if client else cid
            ) or cid
            days = (today - timezone.localtime(e.stage_at).date()).days if e.stage_at else 0
            results.append({
                "id": cid,
                "name": name,
                "detail": f"Waiting {days} day{'s' if days != 1 else ''}",
            })
        return Response({"reason": reason, "count": len(results), "results": results})

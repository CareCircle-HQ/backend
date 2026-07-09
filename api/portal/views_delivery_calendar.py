"""Portal API: a member's delivery calendar.

Backs the member-profile "Delivery Calendar" tab. Returns a summary (cadence,
kitchen, program, authorization window, next delivery, counts) plus every dated
delivery occurrence across the plan's window (past + future).

The calendar is driven by :class:`~api.models.OrderSchedule` (the dated plan
expansion). Each occurrence is enriched with its committed
:class:`~api.models.DeliveryOrder` (matched by client + date) when one exists,
so the row shows the real fulfillment status, proof, and PO number.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response

from ..models import (
    Client,
    DeliveryCadence,
    DeliveryOrder,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    OrderSchedule,
    ScheduleStatus,
)
from ..services.catalog import product_kind_for_enrollment
from .base import PortalAPIView

_WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _order_state(status):
    """Normalize an OrderSchedule.status into a calendar state for coloring."""
    if status == "delivered":
        return "delivered"
    if status == "cancelled":
        return "cancelled"
    if status in ("on_the_kitchen", "on_the_way"):
        return "committed"
    return "scheduled"


def _do_state(status):
    """Normalize a DeliveryOrder.status into a calendar state."""
    if status == "delivered":
        return "delivered"
    if status == "cancelled":
        return "cancelled"
    if status in ("failed", "returned"):
        return "failed"
    return "committed"  # pending / ready_for_delivery / out_for_delivery / on_hold


class MemberDeliveryCalendarView(PortalAPIView):
    """GET /api/portal/members/<client_id>/delivery-calendar/"""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)

        profile_ids = list(
            MemberDietaryProfile.objects.filter(client_id=client_id)
            .values_list("pk", flat=True)
        )

        occurrences = list(
            OrderSchedule.objects.filter(member_id__in=profile_ids)
            .select_related("kitchen", "enrollment")
            .order_by("anticipated_delivery_date")
        ) if profile_ids else []

        # Committed deliveries for this client, keyed by date. Prefer a
        # non-cancelled row when several exist for the same date.
        do_by_date = {}
        for do in (
            DeliveryOrder.objects.filter(member_id=client_id)
            .select_related("purchase_order", "kitchen", "menu_type")
        ):
            d = do.expected_delivery_date
            if d is None:
                continue
            existing = do_by_date.get(d)
            if existing is None or (existing.status == "cancelled" and do.status != "cancelled"):
                do_by_date[d] = do

        today = timezone.localdate()

        rows = []
        counts = {"total": 0, "scheduled": 0, "committed": 0, "delivered": 0,
                  "cancelled": 0, "upcoming": 0}
        next_delivery = None
        for o in occurrences:
            d = o.anticipated_delivery_date
            do = do_by_date.get(d)
            if do is not None:
                state = _do_state(do.status)
                status = do.status
                status_label = do.get_status_display()
                kitchen_name = (do.kitchen.name if do.kitchen else "") or (o.kitchen.name if o.kitchen else "")
                menu_type = (do.menu_type.name if do.menu_type else "") or o.menu_type
                quantity = do.quantity if do.quantity is not None else o.how_many_meals_or_boxes
                po = do.purchase_order
                po_number = po.po_number if po else ""
                po_id = str(po.pk) if po else None
                delivered_at = do.delivered_at.isoformat() if do.delivered_at else None
                proof = list(do.proof_of_delivery or [])
            else:
                state = _order_state(o.status)
                status = o.status
                status_label = o.get_status_display()
                kitchen_name = o.kitchen.name if o.kitchen else ""
                menu_type = o.menu_type
                quantity = o.how_many_meals_or_boxes
                po_number, po_id, delivered_at, proof = "", None, None, []

            counts["total"] += 1
            if state in counts:
                counts[state] += 1
            if d and today and d >= today and state not in ("cancelled", "delivered"):
                counts["upcoming"] += 1
                if next_delivery is None or d < next_delivery:
                    next_delivery = d

            rows.append({
                "date": d.isoformat() if d else None,
                "weekday": d.strftime("%a") if d else "",
                "quantity": quantity,
                "menu_type": menu_type or "",
                "kitchen_name": kitchen_name or "",
                "status": status,
                "status_label": status_label,
                "state": state,
                "committed": do is not None,
                "po_number": po_number,
                "po_id": po_id,
                "delivered_at": delivered_at,
                "proof": proof,
            })

        summary = self._summary(client_id, profile_ids, occurrences)
        summary["next_delivery"] = next_delivery.isoformat() if next_delivery else None
        summary["counts"] = counts

        return Response({"summary": summary, "occurrences": rows})

    def _summary(self, client_id, profile_ids, occurrences):
        plan = (
            MemberDeliverySchedule.objects.filter(
                member_profile_id__in=profile_ids, status=ScheduleStatus.SCHEDULED,
            )
            .select_related("kitchen", "enrollment", "product_type", "program")
            .order_by("-created_at")
            .first()
        ) if profile_ids else None

        enr = plan.enrollment if plan else (occurrences[0].enrollment if occurrences else None)
        kind = product_kind_for_enrollment(enr) if enr else None
        cadence = plan.delivery_days_cadence if plan else ""
        weekdays = (enr.delivery_weekdays if enr and enr.delivery_weekdays else [])

        window_start = plan.starts_on if plan else None
        window_end = plan.ends_on if plan else None
        if window_start is None and occurrences:
            window_start = occurrences[0].anticipated_delivery_date
        if window_end is None and occurrences:
            window_end = occurrences[-1].anticipated_delivery_date

        kitchen = (plan.kitchen if plan and plan.kitchen_id else None) or (enr.kitchen if enr and enr.kitchen_id else None)

        return {
            "kind": kind.value if kind else "",
            "kind_label": kind.label if kind else "",
            "cadence": cadence,
            "cadence_label": dict(DeliveryCadence.choices).get(cadence, ""),
            "weekdays": weekdays,
            "kitchen_name": kitchen.name if kitchen else "",
            "program_name": (plan.program.name if plan and plan.program_id else "")
            or (enr.program_name if enr else ""),
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
        }

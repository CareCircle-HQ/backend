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
    EnrollmentStage,
    HouseholdMember,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    MemberStatus,
    OrderSchedule,
    ScheduleStatus,
)
from ..services.catalog import product_kind_for_enrollment
from ..services.orders import rebuild_delivery_calendar
from .base import PortalAPIView
from .serializers import active_enrollment

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


# Member status -> (calendar state, label) for an UPCOMING scheduled date that
# won't be delivered because the member is currently excluded.
_MEMBER_EXCLUSION = {
    MemberStatus.PAUSED: ("paused", "Paused"),
    MemberStatus.OUT_OF_ORBIT: ("out_of_orbit", "Out of Orbit"),
    MemberStatus.OUT_OF_RANGE: ("out_of_range", "Out of Range"),
    MemberStatus.INACTIVE: ("inactive", "Inactive"),
}
_TERMINAL_STAGES = (
    EnrollmentStage.SERVICE_COMPLETE, EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED,
)
# DEAD enrollments -- closed (e.g. superseded when the governing case was
# replaced), cancelled, or disregarded. Their delivery occurrences are stale
# history and must NOT appear on the member's live calendar (otherwise a member
# reads as served by two kitchens / duplicate service). The ?enrollment=<id>
# override still surfaces a specific dead enrollment's calendar read-only.
_DEAD_ENROLLMENT_STAGES = (
    EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED, EnrollmentStage.DISREGARDED,
)


def _exclusion_state(enrollment, member):
    """Why an upcoming SCHEDULED date won't be delivered, as ``(state, label)``.

    The household On Hold / terminal takes precedence over an individual
    member's Paused / Out-of-Orbit / Out-of-Range / Inactive status. Returns
    ``None`` when nothing excludes the date (a normal Scheduled delivery).
    """
    stage = getattr(enrollment, "stage", None)
    if stage == EnrollmentStage.ON_HOLD:
        return ("on_hold", "On Hold")
    if stage in _TERMINAL_STAGES:
        return ("cancelled", "Service Ended")
    return _MEMBER_EXCLUSION.get(getattr(member, "status", None))


class MemberDeliveryCalendarView(PortalAPIView):
    """GET /api/portal/members/<client_id>/delivery-calendar/"""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)

        # Scope: the PRIMARY's calendar reflects the WHOLE household (every
        # member's deliveries), so the total delivery load is visible in one
        # place. An individual (non-primary) member's calendar shows only their
        # own deliveries.
        membership = (
            HouseholdMember.objects.filter(client_id=client_id)
            .select_related("household")
            .first()
        )
        is_primary = bool(membership and membership.is_primary)
        if is_primary and membership.household_id:
            client_ids = list(
                HouseholdMember.objects.filter(household_id=membership.household_id)
                .values_list("client_id", flat=True)
            )
        else:
            client_ids = [client_id]
        is_household = is_primary and len(client_ids) > 1

        # Optional ?enrollment=<id> scopes the calendar to a SPECIFIC (e.g.
        # superseded/closed) enrollment's own delivery plans instead of the whole
        # household's active profiles -- used to show a prior enrollment's
        # Delivery Schedule tab read-only.
        override = (request.query_params.get("enrollment") or "").strip()
        if override:
            profile_ids = list(
                MemberDietaryProfile.objects.filter(enrollment_id=override)
                .values_list("pk", flat=True)
            )
        else:
            profile_ids = list(
                MemberDietaryProfile.objects.filter(client_id__in=client_ids)
                .values_list("pk", flat=True)
            )

        occ_qs = OrderSchedule.objects.filter(member_id__in=profile_ids)
        if not override:
            # Default (household) calendar reflects only LIVE enrollments; a dead
            # (closed/superseded/cancelled) enrollment's occurrences are stale
            # history -- excluding them stops a member reading as served by two
            # kitchens. A specific dead enrollment is still viewable via
            # ?enrollment=<id>.
            occ_qs = occ_qs.exclude(enrollment__stage__in=_DEAD_ENROLLMENT_STAGES)
        occurrences = list(
            occ_qs.select_related("kitchen", "enrollment", "member")
            .order_by("anticipated_delivery_date")
        ) if profile_ids else []

        # Committed deliveries for the in-scope client(s), keyed by
        # (client, date) so multiple household members on the SAME date don't
        # collide. Prefer a non-cancelled row when several exist for a member+date.
        do_by_key = {}
        for do in (
            DeliveryOrder.objects.filter(member_id__in=client_ids)
            .select_related("purchase_order", "kitchen", "menu_type")
        ):
            d = do.expected_delivery_date
            if d is None:
                continue
            key = (do.member_id, d)
            existing = do_by_key.get(key)
            if existing is None or (existing.status == "cancelled" and do.status != "cancelled"):
                do_by_key[key] = do

        today = timezone.localdate()

        # De-duplicate occurrences per (member, date) in the household aggregate.
        # After a governing-case replacement/restore a member can have BOTH a
        # superseded (closed) enrollment and the active one carrying an occurrence
        # for the SAME date -- which rendered as two rows (e.g. a live "Scheduled"
        # next to the dead plan's "Service Ended", or a real delivery next to a
        # cancelled one). Keep a single row per date, preferring the ACTIVE
        # (non-terminal) enrollment, then a committed delivery over a bare plan.
        # The ?enrollment= read-only view is exempt (it shows one enrollment only).
        if not override:
            def _rank(o):
                mcid = o.member.client_id if o.member_id else None
                terminal = getattr(o.enrollment, "stage", None) in _TERMINAL_STAGES
                has_do = (mcid, o.anticipated_delivery_date) in do_by_key
                # Lower sorts first / wins: active before terminal, committed first.
                return (0 if not terminal else 1, 0 if has_do else 1)

            best = {}
            for o in occurrences:
                mcid = o.member.client_id if o.member_id else None
                key = (mcid, o.anticipated_delivery_date)
                cur = best.get(key)
                if cur is None or _rank(o) < _rank(cur):
                    best[key] = o
            occurrences = sorted(
                best.values(), key=lambda o: (o.anticipated_delivery_date or today)
            )

        rows = []
        counts = {"total": 0, "scheduled": 0, "committed": 0, "delivered": 0,
                  "cancelled": 0, "upcoming": 0}
        next_delivery = None
        for o in occurrences:
            d = o.anticipated_delivery_date
            member_client_id = o.member.client_id if o.member_id else None
            do = do_by_key.get((member_client_id, d))

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

                if o.status == ScheduleStatus.SCHEDULED and d and today:
                    if d >= today:
                        # Overlay the CURRENT exclusion reason onto a still-
                        # scheduled FUTURE date, so the calendar says On Hold /
                        # Paused / Out of Orbit / Out of Range instead of a
                        # misleading "Scheduled". The occurrence is kept (not
                        # deleted) and reverts to Scheduled once the exclusion is
                        # lifted; PO generation still excludes it.
                        ex = _exclusion_state(o.enrollment, o.member)
                        if ex is not None:
                            state, status_label, status = ex[0], ex[1], ex[0]
                    else:
                        # A PAST date still marked Scheduled was never fulfilled
                        # (no delivery order was ever committed for it) -- it has
                        # expired, so don't keep showing a misleading "Scheduled".
                        state, status_label, status = "expired", "Expired", "expired"

            counts["total"] += 1
            if state in counts:
                counts[state] += 1
            # Only genuinely deliverable dates count as "upcoming" / drive the
            # next-delivery date -- an excluded (on-hold / paused / ...) date is
            # shown but is not counted as forthcoming service.
            if d and today and d >= today and state in ("scheduled", "committed"):
                counts["upcoming"] += 1
                if next_delivery is None or d < next_delivery:
                    next_delivery = d

            rows.append({
                "date": d.isoformat() if d else None,
                "weekday": d.strftime("%a") if d else "",
                "member_name": (o.member.member_name if o.member_id else "") or "",
                "member_id": str(member_client_id) if member_client_id else None,
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
        summary["is_household"] = is_household
        summary["member_count"] = len({r["member_id"] for r in rows if r["member_id"]})

        return Response({"summary": summary, "occurrences": rows})

    def post(self, request, client_id):
        """Manually rebuild this household's delivery calendar.

        Creates a delivery plan for any active household member missing one (the
        fix for members added after the first kitchen assignment) and reconciles
        the dated calendar, never touching a date already batched into a PO.
        """
        get_object_or_404(Client, pk=client_id)
        # A scoped (superseded) enrollment is read-only history -- never rebuild it.
        if (request.query_params.get("enrollment") or "").strip():
            return Response(
                {"detail": "This is a previous enrollment (read-only history)."},
                status=400,
            )
        enr = self._enrollment_for(client_id)
        if enr is None:
            return Response(
                {"detail": "This member has no active enrollment, so there is "
                           "no delivery calendar to rebuild."},
                status=400,
            )
        result = rebuild_delivery_calendar(enr)
        return Response({
            "rebuilt": True,
            "plans_created": result.get("plans_created", 0),
            "added": result.get("added", 0),
            "removed": result.get("removed", 0),
            "updated": result.get("updated", 0),
        })

    def _enrollment_for(self, client_id):
        """The LIVE household enrollment this member belongs to, to rebuild.

        Prefers the member's own dietary-profile enrollment (works for non-primary
        members too) but ONLY a non-terminal one -- a client with several
        enrollments can have a CLOSED one that opened most recently, and rebuilding
        a closed enrollment is a no-op (which made the button appear dead). Falls
        back to the client's active/governing enrollment."""
        from api.models import EnrollmentStage

        terminal = {
            EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED,
            EnrollmentStage.DISREGARDED,
        }
        profiles = (
            MemberDietaryProfile.objects.filter(client_id=client_id)
            .select_related("enrollment")
            .order_by("-enrollment__opened_at")
        )
        for p in profiles:
            if p.enrollment_id and EnrollmentStage(p.enrollment.stage) not in terminal:
                return p.enrollment
        client = Client.objects.filter(pk=client_id).first()
        return active_enrollment(client) if client else None

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

"""Cancel FUTURE committed delivery orders for members who are NOT currently in
service.

A DeliveryOrder cut while a member was active isn't retracted when the member is
later PAUSED / pulled Out of Orbit / Out of Range / etc. (e.g. a household ->
individual scope switch pausing the additional members). The already-committed
upcoming delivery then still ships -- and bills -- pushing a household over its
meal cap. PO GENERATION already excludes these members going forward; this cleans
up orders cut BEFORE the status change.

A member is "in service" when they have at least one ACTIVE member profile on a
non-excluded (live) enrollment. Anyone else with a future, not-yet-delivered
order has that order cancelled here.

Also cancels ORPHANED "ghost" orders with no linked member at all (member=None) --
e.g. a profile-only dependent later re-keyed into their own case, or a member
whose Client was deleted -- which can never ship yet show as a member-less line
on the PO.

Review-only by default:
    python manage.py cancel_excluded_member_orders
Apply the fix:
    python manage.py cancel_excluded_member_orders --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    DeliveryOrder,
    DeliveryOrderStatus,
    MemberDietaryProfile,
    MemberStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
)

_TERMINAL = (
    DeliveryOrderStatus.DELIVERED,
    DeliveryOrderStatus.CANCELLED,
    DeliveryOrderStatus.RETURNED,
    DeliveryOrderStatus.FAILED,
)


class Command(BaseCommand):
    help = "Cancel future committed delivery orders for members no longer in service."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Cancel (default: review only).")

    def handle(self, *args, **options):
        apply = options["apply"]
        today = timezone.localdate()

        # Members with at least one ACTIVE profile on a LIVE (non-excluded)
        # enrollment are in service; everyone else is out.
        serving_ids = set(
            MemberDietaryProfile.objects.filter(status=MemberStatus.ACTIVE)
            .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
            .values_list("client_id", flat=True)
        )

        future = (
            DeliveryOrder.objects.filter(
                expected_delivery_date__gte=today, member__isnull=False,
            )
            .exclude(status__in=_TERMINAL)
            .select_related("member", "purchase_order")
        )
        offenders = [do for do in future if do.member_id not in serving_ids]

        # Orphaned "ghost" orders: no linked member at all (member=None) -- e.g. a
        # profile-only dependent later re-keyed into their own case, or a member
        # whose Client was deleted (member is SET_NULL). They can never ship (no
        # recipient) yet appear on the PO as a member-less household line. Cancel
        # every LIVE one, any date.
        orphans = list(
            DeliveryOrder.objects.filter(member__isnull=True)
            .exclude(status__in=_TERMINAL)
            .select_related("purchase_order")
        )

        for do in offenders:
            po = do.purchase_order
            self.stdout.write(
                f"PO {getattr(po, 'po_number', '') or '-'} | member "
                f"{str(do.member_id)[:8]} | {do.expected_delivery_date} | "
                f"{do.status} -> cancel"
            )
        for do in orphans:
            po = do.purchase_order
            self.stdout.write(
                f"PO {getattr(po, 'po_number', '') or '-'} | member <none/ghost> "
                f"| {do.expected_delivery_date} | {do.status} -> cancel"
            )
        self.stdout.write("")
        self.stdout.write(f"Future orders for out-of-service members: {len(offenders)}")
        self.stdout.write(f"Orphaned member-less (ghost) orders       : {len(orphans)}")

        if not apply:
            self.stdout.write("Review only. Re-run with --apply to cancel them.")
            return
        with transaction.atomic():
            n = 0
            for do in offenders + orphans:
                do.status = DeliveryOrderStatus.CANCELLED
                do.save(update_fields=["status", "updated_at"])
                n += 1
        self.stdout.write(self.style.SUCCESS(
            f"Cancelled {n} order(s) at {timezone.now():%Y-%m-%d %H:%M}."
        ))

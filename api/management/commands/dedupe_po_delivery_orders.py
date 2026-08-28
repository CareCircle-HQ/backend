"""Find (and optionally fix) members duplicated on a single Purchase Order.

A member should appear at most once per PO. Historical POs (created before the
`_dedupe_by_client` guard in generate_purchase_order) can carry the SAME client
in two live DeliveryOrders on one PO -- i.e. double meals/boxes. This command
reports those and, with --apply, keeps one live order per (PO, member) and
cancels the rest (audit-preserving; never deletes).

Review-only by default:
    python manage.py dedupe_po_delivery_orders
Apply the fix:
    python manage.py dedupe_po_delivery_orders --apply
"""
import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from api.models import DeliveryOrder, DeliveryOrderStatus, PurchaseOrder

# Fallback sort key for a (theoretically) null created_at, so it sorts oldest.
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


class Command(BaseCommand):
    help = "Report/clean members duplicated (>=2 live delivery orders) on a PO."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Cancel the extra live delivery orders (default: review only).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        # (PO, member) pairs with more than one delivery order overall.
        pairs = (
            DeliveryOrder.objects.filter(member__isnull=False)
            .values("purchase_order_id", "member_id")
            .annotate(n=Count("delivery_order_id"))
            .filter(n__gt=1)
        )

        harmful = 0
        cancelled = 0
        for pair in pairs:
            live = list(
                DeliveryOrder.objects.filter(
                    purchase_order_id=pair["purchase_order_id"],
                    member_id=pair["member_id"],
                )
                .exclude(status=DeliveryOrderStatus.CANCELLED)
                .order_by("created_at", "pk")
            )
            if len(live) < 2:
                continue  # all-cancelled history, or a single live order: fine
            harmful += 1
            po = PurchaseOrder.objects.filter(pk=pair["purchase_order_id"]).first()
            # Keep the most SERVICE-REPRESENTATIVE order, not just the earliest: a
            # duplicate line from a superseded/stale enrollment typically has no
            # kitchen and/or a zero quantity, while the real line (the member's
            # current, case-linked service) carries a kitchen + real quantity.
            # Prefer kitchen-assigned, then positive quantity, then most recent;
            # cancel the rest.
            live.sort(
                key=lambda d: (
                    1 if d.kitchen_id else 0,
                    1 if (d.quantity or 0) > 0 else 0,
                    d.created_at or _EPOCH,
                ),
                reverse=True,
            )
            keep, extras = live[0], live[1:]
            self.stdout.write(
                f"PO {getattr(po, 'po_number', pair['purchase_order_id'])} | "
                f"member {str(pair['member_id'])[:8]} | {len(live)} live "
                f"-> keep 1, cancel {len(extras)}"
            )
            if not apply:
                continue
            with transaction.atomic():
                for do in extras:
                    do.status = DeliveryOrderStatus.CANCELLED
                    do.save(update_fields=["status"])
                    cancelled += 1

        self.stdout.write("")
        self.stdout.write(f"Harmful (PO, member) duplicates: {harmful}")
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"Cancelled {cancelled} duplicate delivery order(s) "
                f"at {timezone.now():%Y-%m-%d %H:%M}."
            ))
        else:
            self.stdout.write(
                "Review only. Re-run with --apply to cancel the extras."
            )

"""Backfill box Purchase Order lines whose quantity is stuck at 0.

Box ``DeliveryOrder.quantity`` (and the source ``OrderSchedule`` occurrence's
``how_many_meals_or_boxes``) can be frozen at 0 when the occurrence was batched
into a PO BEFORE the member's per-delivery box quantity was finalized on their
delivery plan. ``sync_delivery_calendar`` skips batched occurrences and
``resync_scheduled_orders`` historically didn't refresh quantity, so nothing
corrected them -- the kitchen export then printed 0.

This command re-derives the correct box quantity from each member's LIVE
delivery plan (``MemberDeliverySchedule.prod_per_delivery``) and writes it onto
the zero DeliveryOrder lines + their still-SCHEDULED OrderSchedule occurrences.
It ONLY touches boxes and ONLY when the plan carries a positive quantity, so it
can never zero out or inflate a valid value.

Dry-run by default; pass ``--commit`` to persist.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from api.models import DeliveryOrder, MemberDeliverySchedule, OrderSchedule, OrderStatus


class Command(BaseCommand):
    help = "Backfill box PO lines whose quantity is 0 from the member's live plan."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Persist changes (default is a dry run).",
        )
        parser.add_argument(
            "--po", dest="po_number", default="",
            help="Limit to a single PO by po_number (e.g. PO-BOX-2026-W30-K05-2).",
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        po_number = (options.get("po_number") or "").strip()

        orders = DeliveryOrder.objects.filter(
            purchase_order__kind="boxes"
        ).filter(Q(quantity=0) | Q(quantity__isnull=True)).select_related(
            "purchase_order", "member"
        )
        if po_number:
            orders = orders.filter(purchase_order__po_number=po_number)

        # Cache the live box quantity per client (boxes => meals_per_day == 0).
        qty_cache = {}

        def box_qty_for(client_id):
            if client_id in qty_cache:
                return qty_cache[client_id]
            plan = (
                MemberDeliverySchedule.objects.filter(
                    member_profile__client_id=client_id, meals_per_day=0
                )
                .order_by("-updated_at")
                .first()
            )
            qty = (plan.prod_per_delivery or 0) if plan else 0
            qty_cache[client_id] = qty
            return qty

        fixed_do = 0
        skipped = 0
        occ_fixed = 0
        do_updates = []
        occ_keys = []  # (client_id, delivery_date, qty)

        for do in orders:
            client_id = do.member_id
            if client_id is None:
                skipped += 1
                continue
            qty = box_qty_for(client_id)
            if qty <= 0:
                skipped += 1
                self.stdout.write(
                    f"  SKIP do={do.pk} client={client_id}: no positive plan qty"
                )
                continue
            do.quantity = qty
            do_updates.append(do)
            occ_keys.append((client_id, do.expected_delivery_date, qty))
            fixed_do += 1

        self.stdout.write(
            f"Box PO lines with 0/None qty: {orders.count()} | "
            f"will fix: {fixed_do} | skipped (no plan qty): {skipped}"
        )

        if commit and do_updates:
            with transaction.atomic():
                DeliveryOrder.objects.bulk_update(do_updates, ["quantity"])
                # Heal the source OrderSchedule occurrences too, so a later
                # resync / regeneration stays consistent with the PO.
                for client_id, ddate, qty in occ_keys:
                    n = (
                        OrderSchedule.objects.filter(
                            member__client_id=client_id,
                            anticipated_delivery_date=ddate,
                            status=OrderStatus.SCHEDULED,
                        )
                        .filter(Q(how_many_meals_or_boxes=0)
                                | Q(how_many_meals_or_boxes__isnull=True))
                        .update(how_many_meals_or_boxes=qty)
                    )
                    occ_fixed += n
            self.stdout.write(self.style.SUCCESS(
                f"Committed: {fixed_do} DeliveryOrder lines, "
                f"{occ_fixed} OrderSchedule occurrences updated."
            ))
        elif do_updates:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes written. Re-run with --commit to persist."
            ))
        else:
            self.stdout.write("Nothing to fix.")

"""Resolve DOUBLE-SERVING members: a live enrollment on a NON-internal-service
(navigation) case running in parallel with the member's real governing
internal-service enrollment -- each with its own kitchen + calendar + POs, so the
member is shipped from TWO kitchens.

Root cause is fixed going forward (EnrollmentVerificationSerializer no longer
binds non-IS cases). This cleans up the members already double-serving:

  * SURVIVOR  = the governing internal-service enrollment (kept).
  * STRAY     = the live navigation-case enrollment (retired).

For each:
  1. If the survivor has NO delivery address, carry the stray's onto it. If BOTH
     have an address and they differ, it is NOT overwritten -- reported for manual
     reconciliation (the stray sometimes has a more complete unit/apt).
  2. truncate_future_deliveries(stray) -- shortens its plan window so it stops
     generating future deliveries. Already-PO'd (cut) occurrences are preserved.
  3. Disregard the stray enrollment (terminal, case detached).
  4. REPORT (never auto-cancel) any confirmed/ready future PO on the stray's
     kitchen, so an agent can coordinate cancelling the imminent duplicate.

    python manage.py resolve_double_serving_enrollments            # dry run
    python manage.py resolve_double_serving_enrollments --apply
    python manage.py resolve_double_serving_enrollments --client <uuid> --apply
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Retire the stray navigation-case enrollment for double-serving members."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Persist changes (default: dry run).")
        parser.add_argument("--client", default="",
                            help="Limit to a single client_id.")

    def handle(self, *args, **opts):
        from django.db import transaction

        from api.models import (
            CaseType, Client, DeliveryOrder, EnrollmentStage,
            EnrollmentVerification as EV,
        )
        from api.portal import serializers as s
        from api.services.orders import truncate_future_deliveries

        apply = opts["apply"]
        live = {
            EnrollmentStage.KITCHEN_ASSIGNMENT, EnrollmentStage.SERVICE_ACTIVE,
            EnrollmentStage.ON_HOLD,
        }
        _TERMINAL_DO = ("delivered", "cancelled", "returned", "failed")

        stray_qs = EV.objects.filter(
            case__case_type=CaseType.NAVIGATION,
            stage__in=[st.value for st in live],
        ).select_related("client", "kitchen")
        if opts["client"]:
            stray_qs = stray_qs.filter(client_id=opts["client"])

        resolved, skipped = 0, 0
        for stray in stray_qs:
            c = stray.client
            if c is None:
                continue
            gov_case = s.internal_service_case(c)
            survivor = None
            if gov_case is not None:
                survivor = next(
                    (e for e in c.enrollments.all()
                     if e.case_id == gov_case.case_id
                     and EnrollmentStage(e.stage) in live),
                    None,
                )
            if survivor is None:
                # No live IS enrollment to fall back to -> NOT a safe auto-retire
                # (the stray may be the only thing serving them). Leave for review.
                skipped += 1
                self.stdout.write(
                    f"  SKIP {c.client_id}: stray enr {stray.pk} has no live "
                    "internal-service survivor -- review manually."
                )
                continue

            self.stdout.write(f"--- {c.client_id} {c.first_name} {c.last_name}")
            self.stdout.write(
                f"    survivor IS enr {survivor.pk} ({survivor.stage}, "
                f"kitchen={survivor.kitchen.name if survivor.kitchen_id else None}, "
                f"addr={survivor.delivery_address_id}) | "
                f"stray nav enr {stray.pk} ({stray.stage}, "
                f"kitchen={stray.kitchen.name if stray.kitchen_id else None}, "
                f"addr={stray.delivery_address_id})"
            )
            # Address handling.
            carry_addr = (
                survivor.delivery_address_id is None
                and stray.delivery_address_id is not None
            )
            if (
                not carry_addr
                and survivor.delivery_address_id
                and stray.delivery_address_id
                and survivor.delivery_address_id != stray.delivery_address_id
            ):
                self.stdout.write(
                    f"    ADDRESS MISMATCH -- survivor addr {survivor.delivery_address_id} "
                    f"vs stray addr {stray.delivery_address_id}: NOT overwritten, "
                    "reconcile manually (stray may hold the apt/unit)."
                )
            # Report imminent duplicate POs on the stray's kitchen.
            dup_pos = DeliveryOrder.objects.filter(
                member_id=c.client_id, kitchen=stray.kitchen,
            ).exclude(status__in=_TERMINAL_DO).order_by("expected_delivery_date")
            for d in dup_pos[:10]:
                self.stdout.write(
                    f"    ⚠ duplicate PO to cancel manually: DO {str(d.delivery_order_id)[:8]} "
                    f"{stray.kitchen.name if stray.kitchen_id else '?'} "
                    f"{d.status} exp {d.expected_delivery_date}"
                )

            if apply:
                with transaction.atomic():
                    if carry_addr:
                        survivor.delivery_address_id = stray.delivery_address_id
                        if not survivor.delivery_address_verified:
                            survivor.delivery_address_verified = True
                        survivor.save(update_fields=[
                            "delivery_address", "delivery_address_verified",
                        ])
                    try:
                        truncate_future_deliveries(stray)
                    except Exception as exc:  # noqa: BLE001
                        self.stderr.write(f"    truncate failed: {exc}")
                    stray.stage = EnrollmentStage.DISREGARDED
                    stray.close_reason = "double_serving_nav_duplicate"
                    stray.case = None
                    from django.utils import timezone
                    stray.stage_at = timezone.now()
                    if stray.closed_at is None:
                        stray.closed_at = timezone.now()
                    stray.save(update_fields=[
                        "stage", "close_reason", "case", "stage_at", "closed_at",
                    ])
            resolved += 1

        self.stdout.write("")
        self.stdout.write(
            ("APPLIED: " if apply else "DRY RUN (no changes): ")
            + f"resolved {resolved} double-serving member(s), skipped {skipped}."
        )
        if not apply:
            self.stdout.write(
                "Re-run with --apply. Cancel any ⚠ imminent PO manually (coordinate "
                "with the kitchen)."
            )

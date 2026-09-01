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

            # SERVICE-SAFETY GUARD: only retire the stray when the SURVIVOR can
            # take over -- i.e. the survivor holds a live delivery plan too. When
            # the STRAY holds the live plan and the survivor is a hollow shell (no
            # delivery_schedules -- e.g. an IS enrollment that never got a calendar),
            # retiring the stray would strip the member's only deliveries. Skip for
            # manual re-homing (move the plan/kitchen onto the IS enrollment first).
            from django.utils import timezone

            today = timezone.localdate()

            def _serves(e):
                return any(
                    (p.ends_on is None or p.ends_on >= today)
                    for p in e.delivery_schedules.all()
                )

            if _serves(stray) and not _serves(survivor):
                skipped += 1
                self.stdout.write(
                    f"  SKIP {c.client_id}: the STRAY enr {stray.pk} holds the live "
                    f"delivery plan; survivor enr {survivor.pk} has none -- retiring "
                    "would strip service. Re-home the plan/kitchen onto the "
                    "internal-service enrollment first, then retry."
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
            # Address handling -- PREFER the type='delivery' address. The survivor
            # sometimes carries only a 'temporary' (no-unit) capture while the stray
            # holds the proper 'delivery' address (with apt), or vice versa. Newest
            # is NOT a reliable signal here.
            from api.models import Address

            surv_a = (Address.objects.filter(pk=survivor.delivery_address_id).first()
                      if survivor.delivery_address_id else None)
            stray_a = (Address.objects.filter(pk=stray.delivery_address_id).first()
                       if stray.delivery_address_id else None)

            def _is_delivery(a):
                return bool(a) and (a.type or "").lower() == "delivery"

            carry_addr = False
            if surv_a is None and stray_a is not None:
                carry_addr = True  # survivor blank -> take the stray's
            elif surv_a and stray_a and surv_a.pk != stray_a.pk:
                if _is_delivery(stray_a) and not _is_delivery(surv_a):
                    carry_addr = True
                    self.stdout.write(
                        f"    carry delivery-type addr {stray_a.pk} "
                        f"('{stray_a.street}') over survivor's {surv_a.type} "
                        f"addr {surv_a.pk} ('{surv_a.street}')"
                    )
                elif _is_delivery(surv_a):
                    self.stdout.write(
                        f"    survivor already has the delivery-type addr {surv_a.pk} "
                        f"('{surv_a.street}'); keeping it."
                    )
                else:
                    self.stdout.write(
                        f"    ADDRESS MISMATCH (no clear delivery-type) -- survivor "
                        f"{surv_a.pk}/{surv_a.type} vs stray {stray_a.pk}/{stray_a.type}: "
                        "NOT overwritten, reconcile manually."
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

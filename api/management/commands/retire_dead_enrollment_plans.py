"""Retire delivery plans left behind on enrollments that are no longer live.

A ``MemberDeliverySchedule`` belongs to an enrollment. When that enrollment ends --
CLOSED, DISREGARDED, or any other terminal stage -- its plan should stop serving.
Several paths did not do that, so plans were left `scheduled` with a future or NULL
window on dead rows.

Mostly harmless: with no live enrollment nothing generates deliveries, and a survey
on 2026-09-16 found ZERO members being over-delivered off a closed enrollment.

The exception is the case that prompted this. When a member has a live enrollment
AND a dead one whose plan runs a DIFFERENT cadence, PO generation serves BOTH:

    THERESA HOLLAND (7a07db31)
        enr 19845  disregarded      mon_thu   <- dead, still scheduled
        enr 24427  service_active   tue_fri   <- correct
        actual deliveries: Mon 9, Tue 17, Thu 12, Fri 20 over 58 orders

Four deliveries a week instead of two, for over a month, with no alarm.

So the LATENT rows matter: a stale plan is inert only while the member has no live
enrollment. Re-enrol one of them on a different cadence and they silently become
the case above. This retires them all.

Ordering matters. `--apply` shortens each plan's window to yesterday and rebuilds
the calendar, rather than deleting occurrences, because the nightly
``sync_active_calendars`` regenerates from the window -- deleting alone lets them
return.

PO-batched occurrences are deliberately PRESERVED by ``sync_delivery_calendar``, so
this does NOT stop an order already committed to a cut Purchase Order. Those need
``cancel_future_delivery_orders`` (or a targeted cancel, if the member has
legitimate deliveries on other days -- cancelling wholesale would kill those too).
The summary reports how many remain so they are not mistaken for done.

Dry run by default. Nothing is written without ``--apply``.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import (
    DeliveryOrder, EnrollmentStage, EnrollmentVerification, MemberDeliverySchedule,
)

# Stages in which an enrollment may legitimately keep a delivery calendar.
LIVE_STAGES = {
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.ON_HOLD,
}

TERMINAL_ORDER_STATUSES = {"delivered", "cancelled", "returned", "failed"}


class Command(BaseCommand):
    help = "Retire delivery plans stranded on non-live enrollments (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually shorten the plan windows. Without this, only reports.",
        )
        parser.add_argument(
            "--client", default="",
            help="Limit to one client id (for a targeted fix).",
        )
        parser.add_argument(
            "--risk-only", action="store_true",
            help=(
                "Only enrollments whose member ALSO has a live enrollment -- the "
                "cohort that can actually double-deliver. Use this to fix the "
                "harmful rows without touching the inert backlog."
            ),
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        only_client = (options["client"] or "").strip()
        risk_only = options["risk_only"]
        today = timezone.localdate()

        # Grouped by enrollment (truncate_future_deliveries works per enrollment
        # and rebuilds the calendar once, so calling it per PLAN redoes that work).
        # Uses the SAME helper as the StrandedDeliveryPlans metric, so the number
        # the alarm reports and the number this sweep fixes cannot drift -- that
        # duplication has already bitten once, in the unmapped-program metric.
        from api.services.health_metrics import (
            stranded_delivery_plan_enrollments,
        )

        dead = stranded_delivery_plan_enrollments(at_risk=risk_only)
        if only_client:
            dead = {
                k: v for k, v in dead.items()
                if str(v[0].client_id) == only_client
            }

        if not dead:
            self.stdout.write("No stranded plans found.")
            return

        live_clients = {
            str(c) for c in EnrollmentVerification.objects
            .filter(stage__in=LIVE_STAGES).values_list("client_id", flat=True) if c
        }
        at_risk = sum(
            1 for enr, _ in dead.values() if str(enr.client_id) in live_clients
        )
        self.stdout.write(
            f"{len(dead)} dead enrollment(s) hold "
            f"{sum(len(ps) for _, ps in dead.values())} scheduled plan(s)."
        )
        self.stdout.write(
            f"  {at_risk} belong to a member who ALSO has a live enrollment "
            f"-- those can double-deliver."
        )
        if not apply_changes:
            self.stdout.write(self.style.WARNING("DRY RUN -- pass --apply to write."))

        from api.services.orders import truncate_future_deliveries

        retired = 0
        removed = 0
        failed = 0
        for enr, ps in dead.values():
            label = (
                f"enr {enr.pk} {enr.stage} client {str(enr.client_id)[:8]} "
                f"[{', '.join(p.delivery_days_cadence or '-' for p in ps)}]"
            )
            if not apply_changes:
                self.stdout.write(f"  would retire {label}")
                retired += 1
                continue
            try:
                res = truncate_future_deliveries(enr)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
                failed += 1
                self.stderr.write(self.style.ERROR(f"  FAILED {label}: {exc}"))
                continue
            retired += 1
            removed += int(res.get("removed") or 0)
            self.stdout.write(f"  retired {label} -> {res}")

        verb = "retired" if apply_changes else "would retire"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {retired} enrollment(s); {removed} occurrence(s) removed"
            + (f"; {failed} failed" if failed else "")
        ))

        if apply_changes:
            # Shortening a window cannot recall an order already committed to a cut
            # PO. Report them rather than let the sweep look complete.
            stuck = 0
            for enr, _ in dead.values():
                if not enr.client_id:
                    continue
                stuck += sum(
                    1 for o in DeliveryOrder.objects.filter(
                        member_id=enr.client_id, expected_delivery_date__gte=today,
                    ) if o.status not in TERMINAL_ORDER_STATUSES
                )
            if stuck:
                self.stdout.write(self.style.WARNING(
                    f"{stuck} future delivery order(s) remain for these members -- "
                    "PO-committed occurrences are preserved on purpose. Check each: "
                    "some will be LEGITIMATE deliveries on the live plan."
                ))

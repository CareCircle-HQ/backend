"""Move Williamsburg households off Mon/Thu and onto the Wed-Only cadence.

Williamsburg launched on Mon/Thu because that was the only cadence we had. The
Wed-Only cadence (once a week, delivered Wednesday) was added later and is when
these households are actually delivered, so the stored plans have been wrong.

This is the one-off catch-up for households enrolled before the fast-track was
switched over (see ``api.services.williamsburg``, which now builds new
Williamsburg enrollments on Wed-Only).

It reuses the SAME path the Logistics kitchen/cadence edit takes --
``update_household_cadence`` -> ``reconcile_enrollment_calendar`` ->
``resync_scheduled_orders`` -- so the delivery plan, the dated calendar and the
still-scheduled order snapshots all move together. Both calendar steps leave
past and already-PO-batched occurrences alone, so nothing that has shipped is
rewritten.

By default only households that can still be delivered are touched (service
active, on hold, and anything earlier in the pipeline); closed and disregarded
enrollments are historical and left as they are.

Usage:

    python manage.py move_williamsburg_to_wed_only --dry-run
    python manage.py move_williamsburg_to_wed_only
    python manage.py move_williamsburg_to_wed_only --include-closed
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import DeliveryCadence, EnrollmentStage, EnrollmentVerification, Kitchen
from api.services.delivery import current_household_cadence, update_household_cadence
from api.services.orders import reconcile_enrollment_calendar, resync_scheduled_orders
from api.services.williamsburg import (
    WILLIAMSBURG_KITCHEN_NAME,
    WILLIAMSBURG_ONCE_WEEKDAY,
)

# Historical enrollments: no future deliveries to re-plan.
TERMINAL_STAGES = {EnrollmentStage.CLOSED, EnrollmentStage.DISREGARDED}


class Command(BaseCommand):
    help = "Move Williamsburg households from the Mon/Thu cadence to Wed-Only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--include-closed", action="store_true",
            help="Also re-plan closed/disregarded enrollments (normally skipped).",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N households (useful for a first pass).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        include_closed = opts["include_closed"]
        limit = opts["limit"] or 0

        kitchen = Kitchen.objects.filter(
            name__iexact=WILLIAMSBURG_KITCHEN_NAME
        ).first()
        if kitchen is None:
            self.stderr.write(
                self.style.ERROR(f"No kitchen named {WILLIAMSBURG_KITCHEN_NAME!r}.")
            )
            return

        enrollments = (
            EnrollmentVerification.objects.filter(kitchen=kitchen)
            .select_related("case", "client")
            .prefetch_related("delivery_schedules")
            .order_by("pk")
        )
        self.stdout.write(f"Williamsburg enrollments: {enrollments.count()}")

        targets, skipped = [], Counter()
        for enr in enrollments:
            cadence = current_household_cadence(enr)
            if cadence != DeliveryCadence.MON_THU:
                # Anything already on another cadence is left alone: a Wed-Only
                # household is done, and a Tue/Fri one was a deliberate choice
                # this command has no business overriding.
                skipped[f"cadence={cadence or '(no schedule)'}"] += 1
                continue
            if enr.stage in TERMINAL_STAGES and not include_closed:
                skipped[f"stage={enr.stage}"] += 1
                continue
            targets.append(enr)
            if limit and len(targets) >= limit:
                break

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Skipped"))
        for reason, n in skipped.most_common():
            self.stdout.write(f"  {reason:<28} {n:>5}")
        self.stdout.write("")
        self.stdout.write(f"On Mon/Thu and eligible to move: {len(targets)}")

        by_stage = Counter(e.stage for e in targets)
        for stage, n in by_stage.most_common():
            self.stdout.write(f"  {stage:<28} {n:>5}")

        if dry:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — nothing written. {len(targets)} household(s) would move "
                f"to Wed-Only."
            ))
            return

        moved, failed = 0, []
        for enr in targets:
            try:
                with transaction.atomic():
                    update_household_cadence(
                        enr,
                        cadence=DeliveryCadence.ONCE_A_WEEK,
                        once_a_week_weekday=WILLIAMSBURG_ONCE_WEEKDAY,
                        case=enr.case,
                    )
                    # Rebuild the dated calendar exactly like the batch job, then
                    # refresh the still-scheduled snapshots PO generation reads.
                    reconcile_enrollment_calendar(enr)
                    resync_scheduled_orders(enrollment=enr)
                moved += 1
                if moved % 25 == 0:
                    self.stdout.write(f"  {moved}/{len(targets)} moved…")
            except Exception as exc:  # keep going; report at the end
                failed.append((enr.pk, str(exc)[:200]))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Moved {moved} household(s) to Wed-Only."))
        if failed:
            self.stdout.write(self.style.ERROR(f"{len(failed)} failed:"))
            for pk, err in failed[:10]:
                self.stdout.write(f"  {pk}: {err}")

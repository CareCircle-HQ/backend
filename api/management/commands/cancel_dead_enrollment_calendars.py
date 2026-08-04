"""Cancel stale delivery-calendar occurrences left on DEAD enrollments.

When a governing case is replaced the old enrollment is CLOSED, but its delivery
calendar (OrderSchedule occurrences) could linger as SCHEDULED -- so a member
reads as still served, sometimes by a SECOND kitchen (the old enrollment's).
This cancels those SCHEDULED occurrences on closed/cancelled/disregarded
enrollments so only the live enrollment's calendar remains. Never touches
already-committed/delivered occurrences, and never deletes (audit-preserving).

Review-only by default:
    python manage.py cancel_dead_enrollment_calendars
Apply:
    python manage.py cancel_dead_enrollment_calendars --apply
A single client:
    python manage.py cancel_dead_enrollment_calendars --client <client_id> --apply
"""
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from api.models import EnrollmentStage, OrderSchedule, OrderStatus

DEAD_STAGES = (
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.DISREGARDED,
)


class Command(BaseCommand):
    help = "Cancel FUTURE scheduled calendar occurrences left on dead (closed) enrollments."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Cancel them (default: review only).")
        parser.add_argument("--client", default="",
                            help="Limit to one client_id.")
        parser.add_argument(
            "--include-past", action="store_true",
            help="Also cancel PAST scheduled occurrences (default: future only; "
                 "past rows are inert history and left alone).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        qs = (
            OrderSchedule.objects.filter(
                enrollment__stage__in=DEAD_STAGES,
                status=OrderStatus.SCHEDULED,
            )
            .select_related("enrollment", "kitchen", "member__client")
        )
        # A closed enrollment should never have a FUTURE delivery. Past occurrences
        # are inert history (also hidden by the calendar view guard + excluded from
        # POs), so they're left alone unless --include-past is given.
        if not options["include_past"]:
            qs = qs.filter(anticipated_delivery_date__gte=timezone.localdate())
        if options["client"]:
            qs = qs.filter(member__client_id=options["client"])

        # Report grouped by (enrollment, kitchen) so it's easy to eyeball.
        groups = (
            qs.values("enrollment_id", "enrollment__stage", "kitchen_id")
            .annotate(n=Count("order_id"))
            .order_by("enrollment_id")
        )
        total = 0
        for g in groups:
            total += g["n"]
            self.stdout.write(
                f"enr {g['enrollment_id']} ({g['enrollment__stage']}) | "
                f"kitchen {str(g['kitchen_id'])[:8] if g['kitchen_id'] else None} | "
                f"{g['n']} scheduled occurrence(s)"
            )

        if not apply:
            self.stdout.write("")
            self.stdout.write(
                f"Review only: {total} scheduled occurrence(s) on dead enrollments. "
                "Re-run with --apply to cancel."
            )
            return

        cancelled = qs.update(status=OrderStatus.CANCELLED)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Cancelled {cancelled} stale occurrence(s) on dead enrollments."
        ))

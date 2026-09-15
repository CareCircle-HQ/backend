"""Show (and optionally publish) the service-health gauges.

``collect()`` is pure reads, so the default is to PRINT the numbers and publish
nothing -- useful for checking what an alarm would see, and for confirming the
values match ``manage.py diagnose``.

    python manage.py publish_health_metrics             # print only
    python manage.py publish_health_metrics --publish   # send to CloudWatch
"""

from django.conf import settings
from django.core.management.base import BaseCommand


def _stranded_without_cadence():
    """Enrollment ids that are service_active with a kitchen and a servable
    member, but have no delivery_weekdays -- so no script can build them a plan."""
    from api.models import (
        EnrollmentStage, EnrollmentVerification, ScheduleStatus,
        SERVICE_EXCLUDED_MEMBER_STATUSES,
    )

    rows = (
        EnrollmentVerification.objects
        .filter(stage=EnrollmentStage.SERVICE_ACTIVE, kitchen__isnull=False)
        .exclude(delivery_schedules__status=ScheduleStatus.SCHEDULED)
        .distinct().prefetch_related("member_profiles")[:500]
    )
    return [
        e.pk for e in rows
        if not (e.delivery_weekdays or [])
        and any(
            p.status not in SERVICE_EXCLUDED_MEMBER_STATUSES
            for p in e.member_profiles.all()
        )
    ]


class Command(BaseCommand):
    help = "Print the service-health gauges; --publish sends them to CloudWatch."

    def add_arguments(self, parser):
        parser.add_argument(
            "--publish", action="store_true",
            help="Actually send to CloudWatch (needs CLOUDWATCH_METRICS_ENABLED).",
        )

    def handle(self, *args, **opts):
        from api.services.health_metrics import NAMESPACE, collect, publish

        metrics = collect()
        self.stdout.write(f"=== {NAMESPACE} ===")
        for name, (value, unit) in sorted(metrics.items()):
            suffix = "" if unit == "None" else f" {unit.lower()}"
            self.stdout.write(f"  {name:24} {value}{suffix}")

        # The two worth acting on immediately, called out rather than left to the
        # reader: one is repairable right now, the other means stale data on the
        # Data page.
        servable = metrics.get("DeliveryGapsServable", (0, ""))[0]
        repairable = metrics.get("DeliveryGapsRepairable", (0, ""))[0]
        if repairable:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  {repairable} stranded household(s) have a cadence but no "
                    "plan -- fixable now: python manage.py sync_delivery_calendars"
                )
            )
        needs_agent = servable - repairable
        if needs_agent > 0:
            # Named by enrollment id, not member name: this output gets pasted
            # into chats and tickets.
            self.stdout.write(
                self.style.WARNING(
                    f"\n  {needs_agent} stranded household(s) have a servable member "
                    "but NO delivery cadence. No script can fix these -- there is "
                    "nothing to infer delivery days from. An agent must assign a "
                    "cadence on the Programs tab:"
                )
            )
            for pk in _stranded_without_cadence():
                self.stdout.write(f"      enrollment {pk}")
        age = metrics.get("ReadModelAgeHours", (0, ""))[0]
        if age is not None and age > 12:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  read model is {age}h stale -- the Data page is showing "
                    "that old a picture: python manage.py rebuild_enrollment_analytics"
                )
            )

        if not opts["publish"]:
            self.stdout.write("\n(not published -- pass --publish to send)")
            return
        if not settings.CLOUDWATCH_METRICS_ENABLED:
            self.stdout.write(
                self.style.ERROR(
                    "\nCLOUDWATCH_METRICS_ENABLED is not set, so publish() is a "
                    "no-op. Set it in .env (production) and retry."
                )
            )
            return
        sent = publish(metrics)
        self.stdout.write(self.style.SUCCESS(f"\npublished {sent} metrics"))

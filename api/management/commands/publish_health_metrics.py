"""Show (and optionally publish) the service-health gauges.

``collect()`` is pure reads, so the default is to PRINT the numbers and publish
nothing -- useful for checking what an alarm would see, and for confirming the
values match ``manage.py diagnose``.

    python manage.py publish_health_metrics             # print only
    python manage.py publish_health_metrics --publish   # send to CloudWatch
"""

from django.conf import settings
from django.core.management.base import BaseCommand


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
        if servable:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  {servable} stranded household(s) HAVE a servable member "
                    "-- fixable now: python manage.py sync_delivery_calendars"
                )
            )
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

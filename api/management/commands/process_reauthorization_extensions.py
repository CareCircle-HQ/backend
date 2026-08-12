"""Advance parked reauthorization (service-extension) enrollments by the calendar.

A verified household's reauthorization case is parked as a NON-SERVING
``SCHEDULED_EXTENSION`` enrollment until its authorization window begins (see
docs/reauthorization_extension_plan.md). This command performs the date-driven
transitions:

  * ``today >= max(E1, S2)`` -> ACTIVATE: promote the parked extension to Service
    Active and close the current (being-extended) enrollment.
  * ``E1 < today < S2``      -> GAP: complete the current enrollment and pause its
    members until the reauth window begins.
  * otherwise                -> WAITING: the current enrollment keeps serving.

Runs nightly via Celery beat; also safe to run ad-hoc. Dry-run by default.

Usage:
    python manage.py process_reauthorization_extensions            # dry run
    python manage.py process_reauthorization_extensions --apply
    python manage.py process_reauthorization_extensions --client <uuid> --apply
"""
from django.core.management.base import BaseCommand

from api.history import change_context
from api.models import Client
from api.services.lifecycle import process_scheduled_extensions


class Command(BaseCommand):
    help = (
        "Activate / gap-pause parked reauthorization extensions by the calendar. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")
        parser.add_argument(
            "--client", default="", help="Scope to a single client UUID."
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        client = None
        if opts["client"]:
            client = Client.objects.filter(client_id=opts["client"].strip()).first()
            if client is None:
                self.stdout.write(self.style.ERROR("Client not found."))
                return

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Process reauthorization extensions ==="
        ))

        with change_context("system", actor="cron:reauth-extensions"):
            result = process_scheduled_extensions(
                client=client, apply=apply, actor_label="cron:reauth-extensions",
            )

        self.stdout.write(
            f"  activate (window reached): {result['activated']}\n"
            f"  gap-pause (between windows): {result['gapped']}\n"
            f"  still waiting:               {result['waiting']}\n"
            f"  discarded (reauth closed):   {result['discarded']}\n"
            f"  skipped (no window/case):    {result['skipped']}"
        )
        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED."))
        else:
            self.stdout.write(self.style.WARNING("\nDry run -- re-run with --apply."))

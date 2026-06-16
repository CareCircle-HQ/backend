"""Run the daily Unite Us data pull.

Invoke from cron at the configured time (default 02:00), e.g.:

    0 2 * * *  /path/to/.venv/bin/python /path/to/manage.py daily_pull

Options:
    --client-limit N   Only refresh the first N stored clients (smoke testing).
    --provider-id ID   Restrict to a single provider's credential.
    --triggered-by S   Label recorded on the ImportRun (default: cron).
"""

from django.core.management.base import BaseCommand

from api.services.uniteus_import import run_daily_pull


class Command(BaseCommand):
    help = "Run the daily Unite Us API pull (updates clients/cases, raises tickets)."

    def add_arguments(self, parser):
        parser.add_argument("--client-limit", type=int, default=None)
        parser.add_argument("--provider-id", type=str, default=None)
        parser.add_argument("--triggered-by", type=str, default="cron")

    def handle(self, *args, **options):
        run = run_daily_pull(
            triggered_by=options["triggered_by"],
            client_limit=options["client_limit"],
            provider_id=options["provider_id"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"ImportRun {run.pk} {run.status}: "
                f"created={run.created_count} updated={run.updated_count} "
                f"skipped={run.skipped_count} errors={run.error_count}"
            )
        )
        if run.stats:
            self.stdout.write(f"Per-dataset: {run.stats}")
        if run.error_log:
            self.stdout.write(self.style.WARNING(run.error_log[:2000]))

"""Import a Unite Us CSV export from the command line.

This reuses the exact same logic as the Settings > Import web upload
(``api.services.csv_import.run_csv_import``), but runs in-process with no
ALB / nginx / gunicorn request timeouts in the way. Use it for large exports
(the denormalized screening file runs to several hundred MB) or whenever the
web upload would exceed the gateway timeout.

Usage:
    python manage.py import_csv --type clients --path tmp/clients.csv
    python manage.py import_csv --type screening --path /home/ubuntu/screening.csv

``--type`` must be one of the supported export types (clients, screening,
assessments, cases). Re-running is safe: clients/assessments/cases upsert by id
and screenings skip ids that already exist.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from api.services.csv_import import SUPPORTED_EXPORT_TYPES, run_csv_import


class Command(BaseCommand):
    help = "Import a Unite Us CSV export (clients/screening/assessments/cases) from a file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            required=True,
            choices=SUPPORTED_EXPORT_TYPES,
            help=f"Export type. One of: {', '.join(SUPPORTED_EXPORT_TYPES)}.",
        )
        parser.add_argument(
            "--path",
            required=True,
            help="Path to the CSV file to import.",
        )

    def handle(self, *args, **options):
        export_type = options["type"]
        path = options["path"]
        if not os.path.exists(path):
            raise CommandError(f"CSV not found: {path}")

        self.stdout.write(
            f"Importing '{export_type}' from {path} (this may take a while)..."
        )
        with open(path, "rb") as f:
            run = run_csv_import(
                export_type=export_type, file_obj=f, triggered_by="cli",
            )

        msg = (
            f"ImportRun #{run.pk} {run.status}: "
            f"{run.processed_count} processed, {run.created_count} created, "
            f"{run.updated_count} updated, {run.skipped_count} skipped, "
            f"{run.error_count} errors."
        )
        style = self.style.SUCCESS if run.error_count == 0 else self.style.WARNING
        self.stdout.write(style(msg))

        if run.error_log:
            self.stdout.write(self.style.WARNING("First errors:"))
            self.stdout.write(run.error_log[:2000])

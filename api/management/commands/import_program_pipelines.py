"""Import / refresh Program Name -> Case Category mappings (ActiveProgram) from a CSV.

Usage:
    python manage.py import_program_pipelines
    python manage.py import_program_pipelines --path tmp/program_name_pipelines.csv

Upserts by ``program_name`` so it is safe to re-run after editing the CSV.
"""

import csv
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from api.models import ActiveProgram


def _norm(key):
    return (key or "").strip().lower().replace(" ", "_")


class Command(BaseCommand):
    help = "Import/refresh Program Name -> Case Category mappings from a CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=os.path.join(
                settings.BASE_DIR, "tmp", "program_name_pipelines.csv"
            ),
            help="Path to the CSV file (default: tmp/program_name_pipelines.csv).",
        )

    def handle(self, *args, **options):
        path = options["path"]
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f"CSV not found: {path}"))
            return

        created = updated = skipped = 0
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                r = {_norm(k): (v or "").strip() for k, v in row.items()}
                program_name = r.get("program_name", "")
                case_category = r.get("case_category", "")

                if not program_name or not case_category:
                    skipped += 1
                    continue

                defaults = {
                    "main_category": r.get("main_category", ""),
                    "case_category": case_category,
                    "services_category": r.get("services_category", ""),
                }
                # Optional Food/Transportation column; defaults to Food.
                case_type = r.get("case_type", "").casefold()
                if case_type in ActiveProgram.CaseType.values:
                    defaults["case_type"] = case_type

                _, was_created = ActiveProgram.objects.update_or_create(
                    program_name=program_name,
                    defaults=defaults,
                )
                created += int(was_created)
                updated += int(not was_created)

        self.stdout.write(
            self.style.SUCCESS(
                f"ActiveProgram import done: {created} created, "
                f"{updated} updated, {skipped} skipped."
            )
        )

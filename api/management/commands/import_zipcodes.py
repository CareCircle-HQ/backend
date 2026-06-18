"""Import / refresh the allowed-ZIP list from a CSV.

Usage:
    python manage.py import_zipcodes
    python manage.py import_zipcodes --path tmp/ZipCodes.csv

CSV columns: ``ZIP Code, Borough/Neighborhood, SCN, Platform``.
Upserts by ``zip_code`` so it is safe to re-run after editing the CSV.
"""

import csv
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from api.models import AllowedZipCode


def _norm(key):
    return (key or "").strip().lower()


class Command(BaseCommand):
    help = "Import/refresh the allowed-ZIP list from a CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=os.path.join(settings.BASE_DIR, "tmp", "ZipCodes.csv"),
            help="Path to the CSV file (default: tmp/ZipCodes.csv).",
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
                zip_code = (r.get("zip code") or r.get("zip") or "").strip()
                if not zip_code:
                    skipped += 1
                    continue

                _, was_created = AllowedZipCode.objects.update_or_create(
                    zip_code=zip_code,
                    defaults={
                        "borough": r.get("borough/neighborhood", ""),
                        "scn": r.get("scn", ""),
                        "platform": r.get("platform", ""),
                        "is_active": True,
                    },
                )
                created += int(was_created)
                updated += int(not was_created)

        self.stdout.write(
            self.style.SUCCESS(
                f"AllowedZipCode import done: {created} created, "
                f"{updated} updated, {skipped} skipped."
            )
        )

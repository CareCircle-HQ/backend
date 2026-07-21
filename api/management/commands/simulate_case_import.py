"""DRY-RUN case-import simulation -- prints what a cases CSV *would* do, without
writing anything to the database.

Applies the SAME row skips and status/auth/case-type mapping as the real CSV
import (``api.services.csv_import``), but with the STRICT org policy: keep ONLY
cases Met Council MANAGES (provider == Met Council). Every other case -- other
orgs AND blank-organization rows -- is excluded 100%.

For the kept rows it reports (read-only, comparing each row against the current
stored Case):

* created (new) vs updated (existing), and created broken down by case type
  (Eligibility / Internal Service / Navigation);
* per case type, the transitions this import would apply to EXISTING cases:
    - Open -> Closed
    - Pending authorization -> Approved (authorized)
    - Pending authorization -> Denied

Usage
-----
    python manage.py simulate_case_import --file tmp/import/cases_export.csv
"""
import csv
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from api.models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus
from api.serializers import derive_case_type
from api.services.csv_import import map_case_row
from api.services.lifecycle import is_met_council_case

# Case types we track, in display order (External is always excluded).
_TRACKED_TYPES = [
    (CaseType.ELIGIBILITY, "Eligibility"),
    (CaseType.INTERNAL_SERVICE, "Internal Service"),
    (CaseType.NAVIGATION, "Navigation"),
]


class Command(BaseCommand):
    help = "Simulate a cases CSV import (read-only) and print what it would do."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", required=True,
            help="Path to the cases export CSV to simulate.",
        )

    def handle(self, *args, **opts):
        path = opts["file"]
        try:
            fh = open(path, newline="")
        except OSError as exc:
            raise CommandError(f"Cannot open file: {exc}")

        head = self.style.MIGRATE_HEADING
        rows = 0
        skips = defaultdict(int)
        created = updated = 0
        created_by_type = defaultdict(int)
        # transition[metric][case_type] = count
        opened_to_closed = defaultdict(int)
        pending_to_approved = defaultdict(int)
        pending_to_denied = defaultdict(int)

        with fh:
            reader = csv.DictReader(fh)
            if "case_id" not in (reader.fieldnames or []):
                raise CommandError("Not a cases export (no 'case_id' column).")
            for row in reader:
                rows += 1
                cid = (row.get("case_id") or "").strip()
                if not cid:
                    skips["blank case_id"] += 1
                    continue

                # STRICT org policy: keep ONLY Met Council-managed cases. This
                # excludes other orgs AND blank-organization rows.
                prov_id = (row.get("provider_id") or "").strip()
                prov_name = (row.get("provider_name") or "").strip()
                if not is_met_council_case(
                    provider_id=prov_id, provider_name=prov_name,
                    allow_originating=False,
                ):
                    if not (prov_id or prov_name):
                        skips["blank organization (excluded)"] += 1
                    else:
                        skips["other org (excluded)"] += 1
                    continue

                # Referral intake rows are never managed cases.
                if (row.get("case_status") or "").strip().lower() == "referred":
                    skips["referred"] += 1
                    continue
                # No program == never advanced into a Met Council program.
                if not (row.get("program_name") or "").strip():
                    skips["blank program_name"] += 1
                    continue
                case_type = derive_case_type(
                    row.get("service_subtype"), row.get("program_name")
                )
                if case_type == CaseType.EXTERNAL_SERVICE:
                    skips["external service (excluded)"] += 1
                    continue

                # Mapped incoming values (same mapping the real import uses).
                payload = map_case_row(row)
                new_status = payload.get("case_status")
                new_auth = payload.get("service_authorization_status")

                prev = (
                    Case.objects.filter(pk=cid)
                    .values("case_status", "service_authorization_status")
                    .first()
                )
                if prev is None:
                    created += 1
                    created_by_type[case_type] += 1
                    continue

                updated += 1
                # Transitions on the EXISTING case.
                if (
                    prev["case_status"] == CaseStatus.OPEN
                    and new_status == CaseStatus.CLOSED
                ):
                    opened_to_closed[case_type] += 1
                if prev["service_authorization_status"] == ServiceAuthorizationStatus.PENDING:
                    if new_auth == ServiceAuthorizationStatus.APPROVED:
                        pending_to_approved[case_type] += 1
                    elif new_auth == ServiceAuthorizationStatus.DENIED:
                        pending_to_denied[case_type] += 1

        kept = created + updated
        total_skipped = sum(skips.values())

        # ---- report ------------------------------------------------------
        self.stdout.write(head(
            f"\n=== Import simulation: {path} (READ-ONLY, no DB writes) ==="
        ))
        self.stdout.write(f"Rows in file                : {rows:,}")
        self.stdout.write(f"Skipped (not imported)      : {total_skipped:,}")
        for reason, n in sorted(skips.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"    {reason:<32}: {n:,}")
        self.stdout.write(f"Kept (would import)         : {kept:,}")
        self.stdout.write(f"    created (new)            : {created:,}")
        self.stdout.write(f"    updated (existing)       : {updated:,}")

        self.stdout.write(head("\nCreated by case type:"))
        for ct, label in _TRACKED_TYPES:
            self.stdout.write(f"    {label:<18}: {created_by_type.get(ct, 0):,}")

        def _by_type(title, table):
            self.stdout.write(head(f"\n{title}:"))
            total = sum(table.values())
            for ct, label in _TRACKED_TYPES:
                self.stdout.write(f"    {label:<18}: {table.get(ct, 0):,}")
            self.stdout.write(f"    {'TOTAL':<18}: {total:,}")

        self.stdout.write(head("\n--- Transitions on EXISTING cases ---"))
        _by_type("Open -> Closed", opened_to_closed)
        _by_type("Pending authorization -> Approved (authorized)", pending_to_approved)
        _by_type("Pending authorization -> Denied", pending_to_denied)
        self.stdout.write("")

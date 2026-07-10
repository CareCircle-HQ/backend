"""Audit the DB against a flat Williamsburg member list and (with --apply) set
the Williamsburg lead source + flag on the clients that are missing them.

The sheet is a simple two-column list:

    | Unite Us Client ID | Facility |

For every id in the file this command reports one of three buckets:

  * ``missing``      - id NOT in the DB. Can't be created from an id alone, so
                       it's reported as "needs to be added" (manual import).
  * ``needs_update`` - in the DB but its ``lead_source`` isn't "Williamsburg"
                       and/or its ``is_williamsburg`` flag is off. With --apply
                       both are set (lead_source="Williamsburg", flag=True).
  * ``ok``           - in the DB and already lead_source="Williamsburg" + flag.

Unlike ``reconcile_williamsburg_revised`` this does NOT touch households,
enrollments, kitchens or service state -- it only reconciles the lead source
and its derived flag. Dry-run (prints + stats, no writes) unless --apply.

Usage:
    python manage.py sync_williamsburg_lead_source                 # dry run
    python manage.py sync_williamsburg_lead_source --apply         # commit
    python manage.py sync_williamsburg_lead_source --file other.xlsx
"""
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Client

_DEFAULT_FILE = "tmp/verification/Williamsburg members 7.9.26.xlsx"
_ID_COL = "Unite Us Client ID"
_CANONICAL_LEAD_SOURCE = "Williamsburg"


def _norm(value):
    return "" if value is None else str(value).strip()


def _read_ids(path):
    """Return (ordered_unique_ids, total_rows, duplicate_count) from the sheet."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return [], 0, 0
    header = [_norm(c) for c in rows[0]]
    try:
        ci = header.index(_ID_COL)
    except ValueError:
        ci = 0  # fall back to the first column
    seen = set()
    ordered = []
    total = 0
    for r in rows[1:]:
        cid = _norm(r[ci]).lower() if ci < len(r) else ""
        if not cid:
            continue
        total += 1
        if cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)
    return ordered, total, total - len(ordered)


class Command(BaseCommand):
    help = (
        "Audit the DB against a flat Williamsburg member list; set "
        "lead_source='Williamsburg' + is_williamsburg on clients missing them. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Member list .xlsx path.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]

        ids, total_rows, dupes = _read_ids(path)
        if not ids:
            self.stdout.write(self.style.ERROR(f"No client ids read from {path}."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Williamsburg list: {path} -> {total_rows} rows, {len(ids)} unique ids"
        ))

        missing = []       # ids not in DB (need to be added)
        needs_update = []   # (cid, [changes]) in DB but lead source/flag off
        ok = []             # already correct
        report = Counter()
        # Field-level tallies for the stats block.
        lead_source_changes = 0
        flag_changes = 0

        with transaction.atomic():
            for cid in ids:
                client = Client.objects.filter(client_id=cid).first()
                if client is None:
                    missing.append(cid)
                    report["missing"] += 1
                    continue

                changes = []
                if (client.lead_source or "").strip().lower() != _CANONICAL_LEAD_SOURCE.lower():
                    changes.append(
                        f"lead_source '{client.lead_source or '-'}' -> '{_CANONICAL_LEAD_SOURCE}'"
                    )
                    client.lead_source = _CANONICAL_LEAD_SOURCE
                if not client.is_williamsburg:
                    changes.append("is_williamsburg False -> True")
                    client.is_williamsburg = True

                if not changes:
                    ok.append(cid)
                    report["ok"] += 1
                    continue

                needs_update.append((cid, changes))
                report["needs_update"] += 1
                for c in changes:
                    if c.startswith("lead_source"):
                        lead_source_changes += 1
                    else:
                        flag_changes += 1
                client.save(update_fields=[
                    f for f, changed in (
                        ("lead_source", any(c.startswith("lead_source") for c in changes)),
                        ("is_williamsburg", any(c.startswith("is_williamsburg") for c in changes)),
                    ) if changed
                ])

            if not apply:
                transaction.set_rollback(True)

        self._report(
            missing, needs_update, ok, report, apply,
            total_rows, len(ids), dupes, lead_source_changes, flag_changes,
        )

    def _report(self, missing, needs_update, ok, report, apply, total_rows,
                unique_ids, dupes, lead_source_changes, flag_changes):
        head = self.style.MIGRATE_HEADING

        # --- Missing: need to be added -------------------------------------
        self.stdout.write(head(
            f"\n=== NOT in DB (need to be added) -- {len(missing)} ==="
        ))
        for cid in missing:
            self.stdout.write(f"  {cid}")

        # --- Needs update: lead source / flag ------------------------------
        self.stdout.write(head(
            f"\n=== In DB, need lead-source/flag update -- {len(needs_update)} ==="
        ))
        for cid, changes in needs_update:
            self.stdout.write(f"  {cid}: {'; '.join(changes)}")

        # --- Stats ---------------------------------------------------------
        self.stdout.write(head("\n=== Stats ==="))
        stats = [
            ("Rows in file", total_rows),
            ("Unique client ids", unique_ids),
            ("Duplicate rows in file", dupes),
            ("Found in DB", report["ok"] + report["needs_update"]),
            ("Missing (need to be added)", report["missing"]),
            ("Already correct (Williamsburg + flag)", report["ok"]),
            ("Needed update", report["needs_update"]),
            ("  - lead_source set to Williamsburg", lead_source_changes),
            ("  - is_williamsburg flag turned on", flag_changes),
        ]
        for label, value in stats:
            self.stdout.write(f"  {label:<42}: {value}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: no changes written. Re-run with --apply to commit."
            ))

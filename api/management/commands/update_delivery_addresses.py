"""One-off: bulk-update members' DELIVERY address + delivery notes from a flat
sheet (MembersDeliveryAddress.xlsx).

The sheet is authoritative for the delivery destination. For EVERY client row
(no status/stage gating) the command resolves the member's delivery address and
overwrites its street / unit / city / state / zip, and sets the delivery notes
when the sheet provides them.

Target-address resolution per client (first match wins):
  1. the client's latest ``EnrollmentVerification.delivery_address`` (if set) --
     the exact Address the verification UI + kitchen export already read;
  2. else the client's existing ``type=delivery`` Address;
  3. else a NEW ``type=delivery`` Address is created for the client.
Home / current / mailing addresses are never touched.

Notes handling: the sheet's "Address Notes" is written only when non-empty; a
blank notes cell leaves any existing notes untouched (never wipes).

Idempotent: re-running after --apply reports 0 updates. Dry-run unless --apply.

Sheet columns (exact headers):
    Unite Us Client ID | Address - Street | Unit/Apt | City | State |
    Postal Code | Address Notes

Usage:
    python manage.py update_delivery_addresses                 # dry run
    python manage.py update_delivery_addresses --apply         # commit
    python manage.py update_delivery_addresses --file other.xlsx
"""
import uuid
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Address, AddressType, Client, EnrollmentVerification

_DEFAULT_FILE = "tmp/verification/MembersDeliveryAddress.xlsx"

# Exact sheet headers -> logical field.
_COL = {
    "id": "Unite Us Client ID",
    "street": "Address - Street",
    "unit": "Unit/Apt",
    "city": "City",
    "state": "State",
    "zip": "Postal Code",
    "notes": "Address Notes",
}

# Address CharField max lengths (values are truncated + counted to avoid a
# Postgres varchar overflow on save).
_MAXLEN = {"street": 255, "unit": 60, "city": 120, "state": 2, "zip": 10}


def _norm(v):
    """Trim ends but preserve internal newlines (delivery notes are multi-line)."""
    return "" if v is None else str(v).strip()


def _zip(v):
    """Normalize a postal code cell that may be an int (10025) or a string."""
    if v is None:
        return ""
    if isinstance(v, float):
        v = int(v)
    if isinstance(v, int):
        s = str(v)
        return s.zfill(5) if len(s) < 5 else s
    return str(v).strip()


class Command(BaseCommand):
    help = (
        "Bulk-update members' delivery address + delivery notes from "
        "MembersDeliveryAddress.xlsx. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE)
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            self.stdout.write(self.style.ERROR(f"No rows read from {path}."))
            return

        header = [_norm(c) for c in rows[0]]
        idx = {h: i for i, h in enumerate(header)}
        col = {k: idx.get(name) for k, name in _COL.items()}
        missing_cols = [name for k, name in _COL.items() if col[k] is None]
        if missing_cols:
            self.stdout.write(self.style.ERROR(
                f"Sheet is missing expected columns: {missing_cols}"
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Delivery-address roster: {path} -> {len(rows) - 1} data rows "
            f"({'APPLY' if apply else 'DRY-RUN'})"
        ))

        report = Counter()
        self.malformed = []      # (row_no, raw_id)
        self.not_found = []      # raw_id
        self.truncated = Counter()
        samples = []             # first N planned changes for the dry-run preview

        def cell(r, key):
            i = col[key]
            return r[i] if i is not None and i < len(r) else None

        with transaction.atomic():
            for row_no, r in enumerate(rows[1:], start=2):
                raw_id = _norm(cell(r, "id"))
                if not raw_id:
                    report["blank_id"] += 1
                    continue
                try:
                    uuid.UUID(raw_id)
                except ValueError:
                    self.malformed.append((row_no, raw_id))
                    report["malformed"] += 1
                    continue
                client = Client.objects.filter(pk=raw_id).first()
                if client is None:
                    self.not_found.append(raw_id)
                    report["not_found"] += 1
                    continue

                report["matched"] += 1

                new = {
                    "street": _norm(cell(r, "street")),
                    "unit": _norm(cell(r, "unit")),
                    "city": _norm(cell(r, "city")),
                    "state": _norm(cell(r, "state")).upper(),
                    "zip": _zip(cell(r, "zip")),
                }
                for f, maxlen in _MAXLEN.items():
                    if len(new[f]) > maxlen:
                        self.truncated[f] += 1
                        new[f] = new[f][:maxlen]
                note = _norm(cell(r, "notes"))

                # Resolve the target Address.
                created = False
                enr = (
                    EnrollmentVerification.objects
                    .filter(client=client, delivery_address__isnull=False)
                    .select_related("delivery_address")
                    .order_by("-opened_at")
                    .first()
                )
                if enr is not None:
                    addr = enr.delivery_address
                else:
                    addr = (
                        Address.objects
                        .filter(client=client, type=AddressType.DELIVERY)
                        .order_by("-updated_at", "-id")
                        .first()
                    )
                    if addr is None:
                        created = True
                        addr = Address(client=client, type=AddressType.DELIVERY)

                # Compute the diff.
                changed_fields = [f for f in new if getattr(addr, f, "") != new[f]]
                note_changed = bool(note) and (addr.notes or "") != note

                if not created and not changed_fields and not note_changed:
                    report["unchanged"] += 1
                    continue

                for f in new:
                    setattr(addr, f, new[f])
                if note:
                    addr.notes = note
                    report["notes_set"] += 1
                now = timezone.now()
                if created:
                    addr.created_at = now
                addr.updated_at = now

                if created:
                    report["created"] += 1
                else:
                    report["updated"] += 1

                if len(samples) < 15:
                    samples.append(
                        f"  row {row_no} {raw_id[:8]} "
                        f"[{'CREATE' if created else 'UPDATE'}] "
                        f"{new['street']}, {new['unit']} {new['city']} "
                        f"{new['state']} {new['zip']}"
                        + (f" | notes+" if note_changed else "")
                    )

                if apply:
                    addr.save()

            if not apply:
                transaction.set_rollback(True)

        # ---- Report ----------------------------------------------------------
        self.stdout.write("")
        if samples:
            self.stdout.write("Sample planned changes:")
            for line in samples:
                self.stdout.write(line)
            self.stdout.write("")
        self.stdout.write(f"  matched clients : {report['matched']}")
        self.stdout.write(f"  addresses created : {report['created']}")
        self.stdout.write(f"  addresses updated : {report['updated']}")
        self.stdout.write(f"  notes set         : {report['notes_set']}")
        self.stdout.write(f"  unchanged         : {report['unchanged']}")
        self.stdout.write(f"  blank id rows     : {report['blank_id']}")
        self.stdout.write(self.style.WARNING(
            f"  malformed ids     : {report['malformed']}"
        ))
        for row_no, raw in self.malformed[:10]:
            self.stdout.write(f"      row {row_no}: {raw!r}")
        self.stdout.write(self.style.WARNING(
            f"  clients not found : {report['not_found']}"
        ))
        for raw in self.not_found[:10]:
            self.stdout.write(f"      {raw}")
        if self.truncated:
            self.stdout.write(self.style.WARNING(
                f"  truncated fields  : {dict(self.truncated)}"
            ))
        self.stdout.write("")
        if apply:
            self.stdout.write(self.style.SUCCESS("Committed."))
        else:
            self.stdout.write(self.style.NOTICE("Dry-run only — re-run with --apply to commit."))

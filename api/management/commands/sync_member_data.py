"""Non-destructive member data sync from the Kitchen Assignment roster .xlsx.

SCRIPT 1 of 2 (see the discussion on the Kitchen Assignment files):

This pass ONLY refreshes data fields on members that ALREADY have a dietary
profile in the DB. It NEVER changes lifecycle: no enrollment-stage moves, no
member status (active/out-of-orbit), no kitchen-output reconciliation, no
delivery schedules/POs, and no Case authorization status. A second script will
later review every member's full DB record and drive the stages.

Per ``client_id`` found in the DB with an existing profile, and ONLY when the
sheet cell is non-empty (a blank cell never wipes existing DB data):
  * delivery_address -> split into street/apt/city/state/zip (shared parser),
    updated on the member's enrollment,
  * menu_type        -> member profile,
  * allergies        -> member profile food_allergies (known codes; unknown
    labels folded into the restrictions note),
  * food_notes       -> member restrictions note,
  * facility         -> enrollment.kitchen (FK only; no schedules/activation).

Cadence (col I) and case_authorization_status (col H) are intentionally IGNORED
here -- they belong to the lifecycle pass.

Sheet columns are read by LETTER (the file has duplicate header labels):
  A=client_id B=delivery_address C=menu_type D=food_notes E=allergies
  F=kitchen(unused) G=cadence(unused) H=case_authorization_status(ignored)
  I=cadence(ignored) J=facility

Usage:
    python manage.py sync_member_data                      # DRY RUN (rolls back)
    python manage.py sync_member_data --apply              # commit
    python manage.py sync_member_data --limit 100          # first 100 rows
    python manage.py sync_member_data --file path.xlsx --sheet 0
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import (
    Address,
    AddressType,
    Client,
    Kitchen,
    MemberDietaryProfile,
)
from api.services.sheet_import import (
    clean,
    parse_address,
    parse_allergies,
    read_xlsx,
    resolve_kitchen,
)

_DEFAULT_FILE = "tmp/verification/Kitchen Assignment_7.3.26_vADAM.xlsx"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Fixed column letters for this roster layout.
_C_ID = "A"
_C_ADDRESS = "B"
_C_MENU = "C"
_C_FOOD_NOTES = "D"
_C_ALLERGIES = "E"
_C_FACILITY = "J"


class Command(BaseCommand):
    help = (
        "Non-destructive data sync from the Kitchen Assignment roster: update "
        "delivery address, menu type, allergies, food notes and kitchen (facility) "
        "on members that already have a profile. No lifecycle/stage changes. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Path to the .xlsx.")
        parser.add_argument("--sheet", type=int, default=0, help="Worksheet index.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N rows.")

    def handle(self, *args, **options):
        apply = options["apply"]
        try:
            sheets = read_xlsx(options["file"])
        except ValueError as exc:
            raise CommandError(str(exc))
        if options["sheet"] >= len(sheets):
            raise CommandError(
                f"Sheet index {options['sheet']} out of range ({len(sheets)} sheets)."
            )
        rows = sheets[options["sheet"]][1]
        rows = [r for r in rows if _UUID_RE.match((r.get(_C_ID, "") or "").strip())]
        if options["limit"]:
            rows = rows[: options["limit"]]

        self.kitchens_by_norm = {
            k.name.strip().lower(): k for k in Kitchen.objects.all()
        }

        report = Counter()
        fields = Counter()  # per-field update tally
        flags = []

        with transaction.atomic():
            for row in rows:
                cid = (row.get(_C_ID, "") or "").strip()
                try:
                    with transaction.atomic():
                        outcome = self._process_row(row, cid, fields)
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                if outcome[0] not in ("updated", "no_change"):
                    flags.append((cid, outcome[1] if len(outcome) > 1 else ""))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, fields, flags, apply)

    def _process_row(self, row, cid, fields):
        client = Client.objects.filter(client_id=cid).first()
        if client is None:
            return ("skip_not_in_db", "client id not in DB")

        # Operate directly on the member's dietary profile (works whether the
        # client is a primary or a dependent). Take the most recent one.
        profile = (
            MemberDietaryProfile.objects.filter(client=client)
            .select_related("enrollment")
            .order_by("-created_at")
            .first()
        )
        if profile is None:
            return ("skip_no_profile", "no dietary profile (left for lifecycle pass)")

        changed = []          # for the report
        profile_fields = []   # model field names to persist

        # --- menu type (member) ---
        menu = clean(row.get(_C_MENU))
        if menu and profile.menu_type != menu:
            profile.menu_type = menu
            changed.append("menu_type")
            profile_fields.append("menu_type")

        # --- allergies + food notes (member) ---
        codes, unknown = parse_allergies(row.get(_C_ALLERGIES))
        if codes and profile.food_allergies != codes:
            profile.food_allergies = codes
            changed.append("food_allergies")
            profile_fields.append("food_allergies")

        food_notes = clean(row.get(_C_FOOD_NOTES))
        other_bits = []
        if food_notes:
            other_bits.append(food_notes)
        if unknown:
            other_bits.append(f"Allergies: {', '.join(unknown)}")
        if other_bits:
            note = "; ".join(other_bits)
            if profile.other_dietary_restrictions != note:
                profile.other_dietary_restrictions = note
                changed.append("food_notes")
                profile_fields.append("other_dietary_restrictions")

        if profile_fields:
            profile.save(update_fields=profile_fields)

        # --- delivery address + kitchen (enrollment) ---
        enr = profile.enrollment
        if enr is not None:
            if self._update_address(client, enr, row.get(_C_ADDRESS)):
                changed.append("delivery_address")
            facility = clean(row.get(_C_FACILITY))
            if facility:
                kitchen, target = resolve_kitchen(self.kitchens_by_norm, facility)
                if kitchen is None:
                    return ("skip_bad_facility",
                            f"unmapped facility {facility!r} (looked for {target!r})")
                if enr.kitchen_id != kitchen.pk:
                    enr.kitchen = kitchen
                    enr.save(update_fields=["kitchen"])
                    changed.append("kitchen")

        for c in changed:
            fields[c] += 1
        return ("updated",) if changed else ("no_change",)

    def _update_address(self, client, enr, raw):
        """Update the enrollment's delivery address IN PLACE from the sheet.
        Returns True when something changed. Blank/unparseable -> no-op."""
        p = parse_address(raw)
        if not (p.street or p.zip):
            return False
        addr = enr.delivery_address if (
            enr.delivery_address_id
            and enr.delivery_address.type == AddressType.DELIVERY
        ) else None
        new = dict(
            street=p.street[:255], unit=p.unit[:60], city=p.city[:120],
            state=(p.state or "")[:2], zip=p.zip[:10],
        )
        if addr is not None:
            if all(getattr(addr, k) == v for k, v in new.items()):
                return False
            for k, v in new.items():
                setattr(addr, k, v)
            addr.save(update_fields=list(new))
            return True
        addr = Address.objects.create(
            client=client, type=AddressType.DELIVERY, **new
        )
        enr.delivery_address = addr
        enr.save(update_fields=["delivery_address"])
        return True

    def _report(self, report, fields, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Member data sync (non-destructive) ==="))
        order = [
            ("updated", "Members updated"),
            ("no_change", "Already up to date (no change)"),
            ("skip_no_profile", "Skipped: no profile yet (for lifecycle pass)"),
            ("skip_not_in_db", "Skipped: client id not in DB"),
            ("skip_bad_facility", "Skipped: unmapped facility"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<46}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<46}: {sum(report.values())}")

        if fields:
            self.stdout.write(head("\nField updates:"))
            for name in ("menu_type", "food_allergies", "food_notes",
                         "delivery_address", "kitchen"):
                if fields.get(name):
                    self.stdout.write(f"  {name:<46}: {fields[name]}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 30):"))
            for cid, reason in flags[:30]:
                self.stdout.write(f"  {cid or '(blank)'}: {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

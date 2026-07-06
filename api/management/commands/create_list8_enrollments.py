"""LIST 8 enrollment CREATE pass (step 0 of the LIST 8 sequence).

The LIST 8 roster contains many primaries whose enrollment was never created.
This pass creates the missing enrollment SKELETON (household + primary
membership + ``EnrollmentVerification`` at Pending Verification, linked to the
governing internal-service case) so the later passes have something to act on:

    1. create_list8_enrollments   <- THIS (create the missing enrollments)
    2. sync_list8_update          <- fills address / dietary / kitchen / cadence
                                     / kitchen output for every member
    3. reconcile_member_stages    <- drives Pending Verification / Verified /
                                     Kitchen Assignment -> Active by auth + data

It deliberately creates ONLY the skeleton (no dietary/address/kitchen here) --
``sync_list8_update`` owns all the per-field data so the two passes never fight.

An enrollment is created for a row's primary (col A) ONLY when the primary:
  * exists as a Client, and
  * has an internal-service case, and
  * has NO enrollment yet, and
  * is NOT already a NON-primary member of an existing household (they are
    already covered by that household's enrollment -- per product), and
  * is NOT listed as a dependent (HM #2..#9) in any LIST 8 row.

Usage:
    python manage.py create_list8_enrollments               # DRY RUN (rolls back)
    python manage.py create_list8_enrollments --apply        # commit
    python manage.py create_list8_enrollments --limit 100
    python manage.py create_list8_enrollments --file path.xlsx
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import (
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    Household,
    HouseholdMember,
)
from api.management.commands.import_meal_verifications import (
    _clean,
    _client_id,
    _read_rows,
)
from api.portal.serializers import internal_service_case

_DEFAULT_FILE = "tmp/verification/LIST8-Full Update + cadence facility + cleaned addresses.xlsx"

_C_PRIMARY = "A"
_C_TOTAL = "Q"
# HM #2..#9 dependent-id columns (first cell of each 5-col block).
_DEP_ID_COLS = ["R", "W", "AB", "AG", "AL", "AQ", "AV", "BA"]


class Command(BaseCommand):
    help = (
        "LIST 8 create pass: create the missing enrollment skeleton (household + "
        "EnrollmentVerification at Pending Verification, linked to the internal-"
        "service case) for primaries that have a case but no enrollment and are "
        "not already a member of an existing household. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Path to the .xlsx.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N rows.")

    def handle(self, *args, **options):
        apply = options["apply"]
        try:
            rows = _read_rows(options["file"])
        except CommandError:
            raise
        rows = [r for r in rows if _client_id(r.get(_C_PRIMARY))]
        if options["limit"]:
            rows = rows[: options["limit"]]

        # Any client listed as a dependent in ANY row is enrolled under that
        # row's primary, so never create a standalone enrollment for them.
        listed_as_member = set()
        for cells in rows:
            for col in _DEP_ID_COLS:
                mid = _client_id(cells.get(col))
                if mid:
                    listed_as_member.add(mid)

        report = Counter()
        flags = []

        with transaction.atomic():
            for cells in rows:
                pid = _client_id(cells.get(_C_PRIMARY))
                try:
                    with transaction.atomic():
                        outcome = self._process_row(cells, pid, listed_as_member)
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                if outcome[0] in ("error", "skip_no_case"):
                    flags.append((f"{outcome[0]} {pid}", outcome[1] if len(outcome) > 1 else ""))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, flags, apply)

    def _process_row(self, cells, pid, listed_as_member):
        primary = Client.objects.filter(client_id=pid).first()
        if primary is None:
            return ("skip_not_in_db", "primary id not in DB")
        if primary.enrollments.exists():
            return ("skip_already_enrolled", "primary already enrolled")
        if pid in listed_as_member:
            return ("skip_listed_dependent", "listed as a dependent in another row")

        # Product rule: if the primary is already a NON-primary member of an
        # existing household, they are covered by that household's enrollment --
        # do not create a separate one. (An existing PRIMARY membership is reused.)
        membership = (
            HouseholdMember.objects.filter(client=primary)
            .select_related("household")
            .first()
        )
        if membership is not None and not membership.is_primary:
            return ("skip_in_existing_household", "already a member of another household")

        case = internal_service_case(primary)
        if case is None:
            return ("skip_no_case", "no internal-service case")

        if membership is not None:
            household = membership.household
        else:
            household = Household.objects.create(
                name=f"{(primary.last_name or '').strip()} Household".strip()
            )
            HouseholdMember.objects.create(
                household=household, client=primary, is_primary=True
            )

        try:
            total = int(float(_clean(cells.get(_C_TOTAL)) or 1))
        except (TypeError, ValueError):
            total = 1

        program = case.program if case.program_id else None
        EnrollmentVerification.objects.create(
            client=primary,
            household=household,
            case=case,
            program_name=(program.name if program else "") or case.program_name,
            service_type=case.service_type or "",
            household_size=total,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        return ("created",)

    def _report(self, report, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== LIST 8 enrollment create pass ==="))
        order = [
            ("created", "Enrollments created (Pending Verification)"),
            ("skip_already_enrolled", "Skipped: already enrolled"),
            ("skip_in_existing_household", "Skipped: already in an existing household"),
            ("skip_listed_dependent", "Skipped: listed as a dependent in another row"),
            ("skip_no_case", "Skipped: no internal-service case"),
            ("skip_not_in_db", "Skipped: primary id not in DB"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<48}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<48}: {sum(report.values())}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 30):"))
            for pid, reason in flags[:30]:
                self.stdout.write(f"  {pid}: {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

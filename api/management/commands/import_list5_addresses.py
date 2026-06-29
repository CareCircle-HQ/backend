"""Import the "LIST 5 - No Meal Info" sheet.

This sheet carries NO meal data (no output, inputs, cadence or facility) -- the
only useful payload is the delivery address. So every member is brought in at
PENDING VERIFICATION (there is nothing to activate or assign a kitchen for),
with these rules:

  * Require an internal-service case (skip the few without one) -- same gate as
    the other importers.
  * Denied authorization -> the enrollment is set to DENIED (kept as denied),
    not Pending Verification.
  * Blank address -> still created at Pending Verification (no address attached).
  * NEVER override previously-defined household information: an existing
    household is reused as-is; a solo one is created only when none exists.
  * The delivery address is added ONLY when the member has none; an existing
    delivery address is reused (linked to the enrollment), never overwritten.
  * Already-enrolled members are skipped (we never clobber an existing
    enrollment).

Column layout (same as the Trustworthy sheet): A=primary id, B-E=address,
F=address notes, N=total household members.

Usage:
    python manage.py import_list5_addresses --file "tmp/verification/LIST 5 ....xlsx"
    python manage.py import_list5_addresses --file "..." --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    Address,
    AddressType,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    Household,
    HouseholdMember,
    MemberDietaryProfile,
    ServiceAuthorizationStatus,
)
from api.management.commands.import_meal_verifications import _clean, _read_rows
from api.management.commands.import_list2_review import _STATE_FIX
from api.portal.serializers import internal_service_case
from api.services.lifecycle import advance_enrollment, recompute_client_stage

_COL_PRIMARY = "A"
_COL_STREET, _COL_CITY, _COL_STATE, _COL_ZIP, _COL_ADDR_NOTES = "B", "C", "D", "E", "F"
_COL_TOTAL = "N"


class Command(BaseCommand):
    help = (
        "Import the LIST 5 'No Meal Info' sheet: bring members in at Pending "
        "Verification with their delivery address (denied authorizations kept as "
        "Denied), without overriding any existing household or address. Dry-run "
        "unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="First N rows.")

    def handle(self, *args, **options):
        rows = _read_rows(options["file"])
        if options["limit"]:
            rows = rows[: options["limit"]]
        apply = options["apply"]

        report = Counter()
        addr = Counter()
        flags = []

        with transaction.atomic():
            for cells in rows:
                primary_id = _clean(cells.get(_COL_PRIMARY))
                try:
                    with transaction.atomic():
                        key, addr_kind, note = self._process_row(cells, primary_id)
                except Exception as exc:  # isolate a bad row, keep going
                    key, addr_kind, note = ("error", "", str(exc))
                report[key] += 1
                if addr_kind:
                    addr[addr_kind] += 1
                if key.startswith("skip") or key in ("error", "denied"):
                    flags.append((primary_id, note))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, addr, flags, apply)

    def _process_row(self, cells, primary_id):
        if not primary_id:
            return ("skip_no_primary_id", "", "blank primary id")
        primary = Client.objects.filter(client_id=primary_id).first()
        if primary is None:
            return ("skip_primary_not_found", "", "primary id not in DB")
        # Never clobber an existing enrollment.
        if primary.enrollments.exists():
            return ("skip_already_enrolled", "", "primary already enrolled")

        case = internal_service_case(primary)
        if case is None:
            return ("skip_no_internal_case", "", "primary has no internal-service case")

        household = self._reuse_or_create_household(primary)
        if household is None:
            return ("skip_member_of_other_household", "", "primary is a dependent in another household")

        try:
            total = int(float(_clean(cells.get(_COL_TOTAL)) or 1))
        except (TypeError, ValueError):
            total = 1

        address, addr_kind = self._resolve_address(primary, cells)

        program = case.program if case.program_id else None
        enr = EnrollmentVerification.objects.create(
            client=primary,
            household=household,
            case=case,
            program_name=(program.name if program else "") or case.program_name,
            service_type=case.service_type or "",
            delivery_address=address,
            household_size=total,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        # Minimal roster entry (no dietary data on this sheet); verification fills
        # the meal details later.
        MemberDietaryProfile.objects.create(
            enrollment=enr,
            client=primary,
            member_name=f"{primary.first_name or ''} {primary.last_name or ''}".strip(),
        )

        # Denied authorization -> keep the enrollment as Denied.
        if case.service_authorization_status == ServiceAuthorizationStatus.DENIED:
            advance_enrollment(
                enr, EnrollmentStage.DENIED, force=True,
                note="LIST 5 import: case authorization is denied.",
            )
            return ("denied", addr_kind, "case authorization denied")

        # Otherwise leave at Pending Verification.
        recompute_client_stage(primary)
        note = "no address on sheet" if addr_kind == "none" else ""
        return ("pending_verification", addr_kind, note)

    def _reuse_or_create_household(self, primary):
        """Reuse the primary's existing household as-is, or create a solo one when
        none exists. Returns None when the primary is a (non-primary) dependent in
        someone else's household -- we never restructure an existing family."""
        membership = (
            HouseholdMember.objects.filter(client=primary)
            .select_related("household")
            .first()
        )
        if membership is not None:
            if not membership.is_primary:
                return None
            return membership.household
        household = Household.objects.create(
            name=f"{(primary.last_name or '').strip()} Household".strip()
        )
        HouseholdMember.objects.create(
            household=household, client=primary, is_primary=True
        )
        return household

    def _resolve_address(self, primary, cells):
        """Reuse an existing delivery address (never overwrite); otherwise create
        one from the sheet; otherwise (blank) attach none. Returns
        (Address|None, kind) where kind is 'existing' / 'created' / 'none'."""
        existing = (
            Address.objects.filter(client=primary, type=AddressType.DELIVERY)
            .order_by("-updated_at")
            .first()
        )
        if existing is not None:
            return existing, "existing"

        street = _clean(cells.get(_COL_STREET))
        city = _clean(cells.get(_COL_CITY))
        if not (street or city):
            return None, "none"

        raw_state = _clean(cells.get(_COL_STATE))
        state = _STATE_FIX.get(raw_state.lower(), raw_state[:2].upper())
        now = timezone.now()
        addr = Address.objects.create(
            client=primary,
            type=AddressType.DELIVERY,
            street=street[:255],
            city=city[:120],
            state=state[:2],
            zip=_clean(cells.get(_COL_ZIP))[:10],
            notes=_clean(cells.get(_COL_ADDR_NOTES)),
            created_at=now,
            updated_at=now,
        )
        return addr, "created"

    def _report(self, report, addr, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== LIST 5 (No Meal Info) import ==="))
        order = [
            ("pending_verification", "Pending Verification"),
            ("denied", "Denied (case authorization denied)"),
            ("skip_already_enrolled", "Skipped: already enrolled"),
            ("skip_no_internal_case", "Skipped: no internal-service case"),
            ("skip_member_of_other_household", "Skipped: dependent in another household"),
            ("skip_primary_not_found", "Skipped: primary id not found"),
            ("skip_no_primary_id", "Skipped: blank primary id"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<44}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<44}: {sum(report.values())}")

        self.stdout.write(head("\nDelivery addresses:"))
        self.stdout.write(f"  {'created (new)':<44}: {addr.get('created', 0)}")
        self.stdout.write(f"  {'reused existing (not overwritten)':<44}: {addr.get('existing', 0)}")
        self.stdout.write(f"  {'none on sheet (blank)':<44}: {addr.get('none', 0)}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 40):"))
            for pid, note in flags[:40]:
                self.stdout.write(f"  {pid or '(blank)'}: {note}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

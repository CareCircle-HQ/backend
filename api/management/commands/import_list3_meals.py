"""Import the "LIST 3 - MealInputsOutputs" sheet (the activatable set).

Every row already has a trustworthy meal OUTPUT (col G) plus, for most, a
cadence (BM) and facility (BN). We build the household + enrollment + dietary
profiles, then drive each row to its correct stage:

  * Under-enumerated household (col S claims more members than have listed IDs)
        -> left at PENDING VERIFICATION so an agent completes the roster.
  * Authorized + kitchen (facility) + cadence
        -> kitchen assigned + SERVICE ACTIVE. The PRIMARY's kitchen meal type /
           food note are taken DIRECTLY from the sheet outputs (G / H); any
           dependents are run through the meal-rules engine.
  * Authorized but missing cadence/facility
        -> KITCHEN ASSIGNMENT (waiting for a kitchen to be assigned).
  * Not authorized
        -> left at VERIFIED (flagged).

Column mapping (shifted vs the Trustworthy sheet by the Output + AI-RE columns):
  A=primary id, B-E=address, F=address notes, G=Meal Type Output,
  H=Food Note Output, I-L=primary input meal cat/allergies/other-allergy/
  other-restrictions, M=general note, N-R=AI-RE (meal cat/allergies/restrictions/
  other-dietary/notes), S=total members, HM #2..#10 in 5-col blocks from T,
  BM=Cadence (A/B/Boxes), BN=Facility (ENG/AST/Boxes->Hicksville).

The PRIMARY's dietary profile is captured from the AI-RE columns (N-R);
dependents (HM #2..#10) use their raw blocks.

Usage:
    python manage.py import_list3_meals --file "tmp/verification/LIST 3 ....xlsx"
    python manage.py import_list3_meals --file "..." --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    Address,
    AddressType,
    Client,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    Household,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MemberStatus,
    ProductTypeKind,
    ServiceAuthorizationStatus,
)
from api.management.commands.import_meal_verifications import (
    _clean,
    _parse_meal_category,
    _parse_restrictions,
    _profile_fields,
    _read_rows,
)
from api.management.commands.import_list2_review import (
    _ai_allergies,
    _RESTR_BLANKS,
    _STATE_FIX,
)
from api.portal.serializers import internal_service_case
from api.services.delivery import create_member_delivery_schedules
from api.services.lifecycle import advance_enrollment
from api.services.meal_rules import apply_to_member
from api.services.orders import generate_delivery_calendar

_COL_PRIMARY = "A"
_COL_STREET, _COL_CITY, _COL_STATE, _COL_ZIP, _COL_ADDR_NOTES = "B", "C", "D", "E", "F"
_COL_MEAL_OUTPUT, _COL_FOOD_NOTE_OUTPUT = "G", "H"
_COL_MEAL_CAT_INPUT = "I"  # used only to detect meals vs boxes
# Primary dietary from the AI-RE columns.
_AI_MENU, _AI_ALLERGY, _AI_RESTR, _AI_OTHER, _AI_NOTES = "N", "O", "P", "Q", "R"
_COL_TOTAL = "S"
# Dependent HM #2..#10 raw 5-col blocks: (id, meal_cat, allergies, other, restr).
_DEP_BLOCKS = [
    ("T", "U", "V", "W", "X"),
    ("Y", "Z", "AA", "AB", "AC"),
    ("AD", "AE", "AF", "AG", "AH"),
    ("AI", "AJ", "AK", "AL", "AM"),
    ("AN", "AO", "AP", "AQ", "AR"),
    ("AS", "AT", "AU", "AV", "AW"),
    ("AX", "AY", "AZ", "BA", "BB"),
    ("BC", "BD", "BE", "BF", "BG"),
    ("BH", "BI", "BJ", "BK", "BL"),
]
_COL_CADENCE, _COL_FACILITY = "BM", "BN"

_FACILITY_TO_KITCHEN = {"eng": "ENG", "ast": "AST", "boxes": "Hicksville", "hicksville": "Hicksville"}
_AUTHORIZED = {
    ServiceAuthorizationStatus.APPROVED,
    ServiceAuthorizationStatus.NOT_REQUIRED,
}


def _primary_profile_fields(cells):
    """Primary dietary from the AI-RE columns (N-R)."""
    menu, category, _kind = _parse_meal_category(cells.get(_AI_MENU))
    food, unknown_food = _ai_allergies(cells.get(_AI_ALLERGY))

    q = _clean(cells.get(_AI_RESTR))
    restrictions, other_restr = ([], [])
    if q.lower() not in _RESTR_BLANKS:
        restrictions, other_restr = _parse_restrictions(q)

    other_bits = []
    other_allergens = _clean(cells.get(_AI_OTHER))
    if other_allergens:
        other_bits.append(f"Other allergens: {other_allergens}")
    if unknown_food:
        other_bits.append(f"Allergies: {', '.join(unknown_food)}")
    if other_restr:
        other_bits.append("; ".join(other_restr))

    return {
        "menu_type": menu,
        "meal_category": category,
        "food_allergies": food,
        "dietary_restrictions": restrictions,
        "other_dietary_restrictions": "; ".join(other_bits),
        "general_verification_notes": _clean(cells.get(_AI_NOTES)),
    }


class Command(BaseCommand):
    help = (
        "Import the LIST 3 'MealInputsOutputs' sheet: build households + dietary "
        "profiles, assign kitchen + cadence and activate authorized rows (primary "
        "outputs from cols G/H), send authorized rows missing cadence/facility to "
        "Kitchen Assignment, and leave under-enumerated households at Pending "
        "Verification. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="First N rows.")
        parser.add_argument(
            "--cadence-a",
            choices=["mon_thu", "tue_fri"],
            default="mon_thu",
            help="Which meal cadence sheet code 'A' maps to ('B' gets the other).",
        )

    def handle(self, *args, **options):
        rows = _read_rows(options["file"])
        if options["limit"]:
            rows = rows[: options["limit"]]
        apply = options["apply"]

        cad_a = (
            DeliveryCadence.MON_THU
            if options["cadence_a"] == "mon_thu"
            else DeliveryCadence.TUE_FRI
        )
        cad_b = (
            DeliveryCadence.TUE_FRI
            if cad_a == DeliveryCadence.MON_THU
            else DeliveryCadence.MON_THU
        )
        self.cadence_map = {"a": cad_a, "b": cad_b}
        self.kitchens = {k.name: k for k in Kitchen.objects.all()}

        # Clients listed as a dependent in any row -> skip their own primary row.
        listed_as_member = set()
        for cells in rows:
            for block in _DEP_BLOCKS:
                mid = _clean(cells.get(block[0]))
                if mid:
                    listed_as_member.add(mid)

        report = Counter()
        flags = []

        with transaction.atomic():
            for cells in rows:
                primary_id = _clean(cells.get(_COL_PRIMARY))
                try:
                    with transaction.atomic():
                        key, note = self._process_row(
                            cells, primary_id, listed_as_member
                        )
                except Exception as exc:  # isolate a bad row, keep going
                    key, note = ("error", str(exc))
                report[key] += 1
                if key not in ("activated", "kitchen_assignment"):
                    flags.append((primary_id, f"{key}: {note}" if note else key))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, flags, apply, options["cadence_a"])

    def _process_row(self, cells, primary_id, listed_as_member):
        if not primary_id:
            return ("skip_no_primary_id", "")
        primary = Client.objects.filter(client_id=primary_id).first()
        if primary is None:
            return ("skip_primary_not_found", "primary id not in DB")
        if primary.enrollments.exists():
            return ("skip_already_enrolled", "primary already enrolled")
        if primary_id in listed_as_member:
            return ("skip_member_of_other_household", "listed as a dependent elsewhere")

        case = internal_service_case(primary)
        if case is None:
            return ("skip_no_internal_case", "primary has no internal-service case")

        household, member_clients, block_for = self._build_household(
            primary, primary_id, cells
        )
        if household is None:
            return ("skip_member_of_other_household", "primary is a dependent in another household")

        try:
            total = int(float(_clean(cells.get(_COL_TOTAL)) or 1))
        except (TypeError, ValueError):
            total = 1
        listed_deps = sum(1 for blk in _DEP_BLOCKS if _clean(cells.get(blk[0])))
        under_enumerated = total > 1 and listed_deps < (total - 1)

        program = case.program if case.program_id else None
        enr = EnrollmentVerification.objects.create(
            client=primary,
            household=household,
            case=case,
            program_name=(program.name if program else "") or case.program_name,
            service_type=case.service_type or "",
            delivery_address=self._delivery_address(primary, cells),
            household_size=total,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )

        for m in member_clients:
            if m is primary:
                fields = _primary_profile_fields(cells)
            else:
                fields, _kind = _profile_fields(block_for[str(m.client_id)], cells)
            MemberDietaryProfile.objects.create(
                enrollment=enr,
                client=m,
                member_name=f"{m.first_name or ''} {m.last_name or ''}".strip(),
                **fields,
            )

        # Under-enumerated households stay at Pending Verification for an agent
        # to complete the roster before service.
        if under_enumerated:
            return ("pending_verification_household", f"claims {total}, lists {listed_deps}")

        advance_enrollment(
            enr, EnrollmentStage.VERIFIED, force=True,
            note="Imported from LIST 3 (MealInputsOutputs).",
        )

        authorized = case.service_authorization_status in _AUTHORIZED
        if not authorized:
            return ("verified_unauthorized", "case not authorized")

        # Kitchen + cadence resolution.
        facility = _clean(cells.get(_COL_FACILITY)).lower()
        kitchen = self.kitchens.get(_FACILITY_TO_KITCHEN.get(facility, ""))
        cadence_code = _clean(cells.get(_COL_CADENCE)).lower()
        _menu, _cat, kind_from_input = _parse_meal_category(cells.get(_COL_MEAL_CAT_INPUT))
        is_boxes = (
            kind_from_input == ProductTypeKind.BOXES
            or facility == "boxes"
            or cadence_code == "boxes"
        )
        primary_kind = ProductTypeKind.BOXES if is_boxes else ProductTypeKind.MEALS
        meal_cadence = self.cadence_map.get(cadence_code)
        has_cadence = is_boxes or meal_cadence is not None

        if kitchen is None or not has_cadence:
            advance_enrollment(
                enr, EnrollmentStage.KITCHEN_ASSIGNMENT, force=True,
                note="Imported; verified, awaiting kitchen assignment.",
            )
            return ("kitchen_assignment", "no kitchen/cadence" if kitchen is None else "no cadence")

        # --- Activate: kitchen assigned + service active ---
        enr.kitchen = kitchen
        enr.save(update_fields=["kitchen"])
        meal_output = _clean(cells.get(_COL_MEAL_OUTPUT))
        food_note = _clean(cells.get(_COL_FOOD_NOTE_OUTPUT))
        for profile in enr.member_profiles.all():
            if profile.client_id == primary.client_id:
                # The primary's kitchen meal type / food note come straight from
                # the sheet's trustworthy outputs (G/H).
                profile.status = MemberStatus.ACTIVE
                profile.kitchen_meal_type = meal_output
                profile.kitchen_food_notes = food_note
                profile.save(update_fields=[
                    "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
                ])
            else:
                apply_to_member(profile)
        create_member_delivery_schedules(
            enr,
            case=case,
            cadence=(meal_cadence or DeliveryCadence.ONCE_A_WEEK),
            kitchen=kitchen,
            product_kind=primary_kind,
        )
        generate_delivery_calendar(enr)
        advance_enrollment(
            enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
            note=f"Imported; kitchen assigned ({kitchen.name}); service activated.",
        )
        return ("activated", "")

    def _build_household(self, primary, primary_id, cells):
        primary_membership = (
            HouseholdMember.objects.filter(client=primary)
            .select_related("household")
            .first()
        )
        if primary_membership is not None and not primary_membership.is_primary:
            return None, [], {}

        if primary_membership is not None:
            household = primary_membership.household
        else:
            household = Household.objects.create(
                name=f"{(primary.last_name or '').strip()} Household".strip()
            )
            HouseholdMember.objects.create(
                household=household, client=primary, is_primary=True
            )

        member_clients = [primary]
        block_for = {}
        for block in _DEP_BLOCKS:
            mid = _clean(cells.get(block[0]))
            if not mid or mid == primary_id or str(mid) in block_for:
                continue
            c = Client.objects.filter(client_id=mid).first()
            if c is None:
                continue
            existing = (
                HouseholdMember.objects.filter(client=c)
                .select_related("household")
                .first()
            )
            if existing is not None and existing.household_id == household.household_id:
                pass
            elif existing is not None:
                old = existing.household
                if (
                    existing.is_primary
                    and old.members.count() == 1
                    and not old.enrollment_verifications.exists()
                ):
                    existing.household = household
                    existing.is_primary = False
                    existing.save(update_fields=["household", "is_primary"])
                    old.delete()
                else:
                    continue
            else:
                HouseholdMember.objects.create(
                    household=household, client=c, is_primary=False
                )
            member_clients.append(c)
            block_for[str(c.client_id)] = block
        return household, member_clients, block_for

    def _delivery_address(self, primary, cells):
        street = _clean(cells.get(_COL_STREET))
        city = _clean(cells.get(_COL_CITY))
        if not (street or city):
            return None
        raw_state = _clean(cells.get(_COL_STATE))
        state = _STATE_FIX.get(raw_state.lower(), raw_state[:2].upper())
        now = timezone.now()
        return Address.objects.create(
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

    def _report(self, report, flags, apply, cadence_a):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== LIST 3 meal import ==="))
        self.stdout.write(f"Cadence 'A' -> {cadence_a} ('B' -> the other)")
        order = [
            ("activated", "Activated (kitchen + service active)"),
            ("kitchen_assignment", "Kitchen Assignment (awaiting kitchen)"),
            ("pending_verification_household", "Pending Verification (under-enumerated household)"),
            ("verified_unauthorized", "Verified only (case not authorized)"),
            ("skip_already_enrolled", "Skipped: already enrolled"),
            ("skip_no_internal_case", "Skipped: no internal-service case"),
            ("skip_member_of_other_household", "Skipped: dependent in another household"),
            ("skip_primary_not_found", "Skipped: primary id not found"),
            ("skip_no_primary_id", "Skipped: blank primary id"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<48}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<48}: {sum(report.values())}")

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

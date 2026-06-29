"""Import household meal-verification data from the "Meal Inputs" Excel export.

One sheet row = one household, keyed by the primary's Unite Us Client ID (col A).
For each qualifying row we build the household + verification + per-member dietary
profiles, then (when the case is authorized and a kitchen/cadence is known) assign
the kitchen and activate service so the whole household goes Active.

Rules (confirmed with product):
  * The PRIMARY (col A) must have an Internal Service case, else the row is skipped.
  * Dependents (HM #2..#10) do NOT need their own case; they move with the primary.
  * A client already in another household (DB membership, or listed as a member in
    another row) is never made a primary of a new household — those rows are skipped.
  * Quantity per delivery comes from the ProductType catalog (meals 3/day, box 1),
    NOT the sheet. Boxes always ship Wednesday; meals use the row's cadence.

Column mapping:
  A=primary id, B-E=address, F=address notes, I/J/K/L=primary meal cat / allergies /
  other-allergy / other-restrictions, M=general note, N=total members,
  HM#2..#10 in 5-col blocks (id, meal category, food allergies, other allergies,
  other restrictions), BH=Cadence (A->Mon/Thu, B->Tue/Fri, Boxes), BI=Facility
  (ENG, AST, Boxes->Hicksvile).

Usage:
    python manage.py import_meal_verifications                 # DRY RUN (rolls back)
    python manage.py import_meal_verifications --apply          # commit
    python manage.py import_meal_verifications --limit 50       # first 50 rows
    python manage.py import_meal_verifications --file path.xlsx
"""
import re
import zipfile
from collections import Counter
from xml.etree import ElementTree as ET

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import (
    Address,
    AddressType,
    Client,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    Household,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MenuCategory,
    ProductTypeKind,
    ServiceAuthorizationStatus,
)
from api.portal.serializers import internal_service_case
from api.services.delivery import create_member_delivery_schedules
from api.services.lifecycle import advance_enrollment
from api.services.meal_rules import apply_to_member
from api.services.orders import generate_delivery_calendar

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DEFAULT_FILE = "tmp/verification/Meal Inputs Trustworthy.xlsx"

# Per-member column blocks: (id, meal_category, food_allergies, other_allergies,
# other_restrictions). Index 0 is the primary; 1..9 are HM #2..#10.
_MEMBER_BLOCKS = [
    ("A", "I", "J", "K", "L"),
    ("O", "P", "Q", "R", "S"),
    ("T", "U", "V", "W", "X"),
    ("Y", "Z", "AA", "AB", "AC"),
    ("AD", "AE", "AF", "AG", "AH"),
    ("AI", "AJ", "AK", "AL", "AM"),
    ("AN", "AO", "AP", "AQ", "AR"),
    ("AS", "AT", "AU", "AV", "AW"),
    ("AX", "AY", "AZ", "BA", "BB"),
    ("BC", "BD", "BE", "BF", "BG"),
]
_COL_STREET, _COL_CITY, _COL_STATE, _COL_ZIP, _COL_ADDR_NOTES = "B", "C", "D", "E", "F"
_COL_TOTAL, _COL_CADENCE, _COL_FACILITY = "N", "BH", "BI"

# Sheet Facility code -> Kitchen.name.
_FACILITY_TO_KITCHEN = {"eng": "ENG", "ast": "AST", "boxes": "Hicksvile"}
# Sheet Cadence code -> meal DeliveryCadence (boxes/blank handled separately).
_CADENCE_TO_DELIVERY = {"a": DeliveryCadence.MON_THU, "b": DeliveryCadence.TUE_FRI}

# Allergy label (lowercased) -> FoodAllergy code, built from the model choices.
_ALLERGY_BY_LABEL = {label.lower(): code for code, label in FoodAllergy.choices}
# "Other Restrictions" label -> DietaryRestriction code.
_RESTRICTION_BY_LABEL = {
    "diabetic": "diabetes",
    "diabetes": "diabetes",
    "cardiometabolic": "cardio_metabolic",
    "cardio-metabolic": "cardio_metabolic",
    "cardio metabolic": "cardio_metabolic",
    "postpartum": "postpartum",
}
# Meal-category keyword -> (catalog MenuType name, MenuCategory enum or "").
_CATEGORY_KEYWORDS = [
    ("dairy free", ("Dairy Free", MenuCategory.DAIRY_FREE)),
    ("vegetarian", ("Vegetarian", MenuCategory.VEGETARIAN)),
    ("halal", ("Halal", "")),
    ("kosher", ("Kosher", "")),
    ("fresh", ("Standard", MenuCategory.FRESH_MEAL)),
]
_AUTHORIZED = {ServiceAuthorizationStatus.APPROVED, ServiceAuthorizationStatus.NOT_REQUIRED}
_BLANKS = {"", "#n/a", "n/a", "none"}


def _col(ref):
    return re.match(r"[A-Z]+", ref).group(0)


def _read_rows(path):
    """Parse the first worksheet into a list of {column_letter: value} dicts
    (header row excluded). Pure stdlib so no openpyxl/pandas dependency."""
    try:
        z = zipfile.ZipFile(path)
    except (FileNotFoundError, zipfile.BadZipFile) as exc:
        raise CommandError(f"Cannot open workbook {path!r}: {exc}")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in tree.findall(_NS + "si"):
            shared.append("".join(n.text or "" for n in si.iter(_NS + "t")))
    sheets = sorted(
        n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n)
    )
    ws = ET.fromstring(z.read(sheets[0]))
    rows = []
    for r in ws.iter(_NS + "row"):
        if r.get("r") == "1":
            continue
        cells = {}
        for c in r.findall(_NS + "c"):
            v = c.find(_NS + "v")
            if v is None:
                continue
            val = shared[int(v.text)] if c.get("t") == "s" else v.text
            cells[_col(c.get("r"))] = (val or "").strip()
        if cells:
            rows.append(cells)
    return rows


def _clean(value):
    v = (value or "").strip().strip('"')
    return "" if v.lower() in _BLANKS else v


def _split_tokens(value):
    return [t.strip().strip('"') for t in re.split(r"[;,]", value or "") if t.strip()]


def _parse_meal_category(raw):
    """'132 - Fresh (Meals)' -> ('Standard', 'fresh_meal', ProductTypeKind.MEALS)."""
    low = (raw or "").lower()
    kind = None
    if "(boxes)" in low or "box" in low:
        kind = ProductTypeKind.BOXES
    elif "(meals)" in low or "meal" in low:
        kind = ProductTypeKind.MEALS
    menu, category = "", ""
    for keyword, (menu_name, cat) in _CATEGORY_KEYWORDS:
        if keyword in low:
            menu, category = menu_name, cat
            break
    return menu, category, kind


def _parse_allergies(raw):
    codes, unknown = [], []
    for tok in _split_tokens(raw):
        low = tok.lower()
        if low in _BLANKS:
            continue
        code = _ALLERGY_BY_LABEL.get(low)
        if code and code != "none":
            codes.append(code)
        else:
            unknown.append(tok)
    return list(dict.fromkeys(codes)), unknown


def _parse_restrictions(raw):
    codes, other = [], []
    for tok in _split_tokens(raw):
        if tok.lower() in _BLANKS:
            continue
        code = _RESTRICTION_BY_LABEL.get(tok.lower())
        if code:
            codes.append(code)
        else:
            other.append(tok)
    return list(dict.fromkeys(codes)), other


def _profile_fields(block, cells):
    _id, mc, fa, oa, orr = block
    menu, category, kind = _parse_meal_category(cells.get(mc))
    food, unknown_food = _parse_allergies(cells.get(fa))
    restrictions, other_restr = _parse_restrictions(cells.get(orr))
    other_bits = []
    extra_allergy = _clean(cells.get(oa))
    if extra_allergy:
        other_bits.append(f"Other allergies: {extra_allergy}")
    if unknown_food:
        other_bits.append(f"Allergies: {', '.join(unknown_food)}")
    if other_restr:
        other_bits.append("; ".join(other_restr))
    fields = {
        "menu_type": menu,
        "meal_category": category,
        "food_allergies": food,
        "dietary_restrictions": restrictions,
        "other_dietary_restrictions": "; ".join(other_bits),
    }
    return fields, kind


class Command(BaseCommand):
    help = (
        "Import household meal-verification data from the Meal Inputs Excel sheet: "
        "build households, verifications and member dietary profiles, then assign "
        "kitchens and activate authorized households. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Path to the .xlsx.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N rows.")
        parser.add_argument(
            "--cadence-a",
            choices=["mon_thu", "tue_fri"],
            default="mon_thu",
            help="Which meal cadence sheet code 'A' maps to (B gets the other).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        rows = _read_rows(options["file"])
        if options["limit"]:
            rows = rows[: options["limit"]]

        # Cadence A/B direction is configurable.
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
        cadence_map = {"a": cad_a, "b": cad_b}

        kitchens = {k.name: k for k in Kitchen.objects.all()}

        # Pass 1: every client id listed as a household MEMBER (HM #2..#10) in any
        # row — used to skip their own single-member row (they belong to a household).
        listed_as_member = set()
        for cells in rows:
            for block in _MEMBER_BLOCKS[1:]:
                mid = _clean(cells.get(block[0]))
                if mid:
                    listed_as_member.add(mid)

        report = Counter()
        verified_reasons = Counter()
        flags = []  # (primary_id, reason)

        with transaction.atomic():
            for cells in rows:
                primary_id = _clean(cells.get("A"))
                try:
                    with transaction.atomic():
                        outcome = self._process_row(
                            cells, primary_id, kitchens, cadence_map, listed_as_member
                        )
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                reason = outcome[1] if len(outcome) > 1 else ""
                if outcome[0] == "verified_only":
                    verified_reasons[reason] += 1
                elif outcome[0] != "activated":
                    flags.append((primary_id, reason))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, verified_reasons, flags, apply, options["cadence_a"])

    def _process_row(self, cells, primary_id, kitchens, cadence_map, listed_as_member):
        if not primary_id:
            return ("skip_no_primary_id",)
        primary = Client.objects.filter(client_id=primary_id).first()
        if primary is None:
            return ("skip_primary_not_found", "primary id not in DB")
        if primary.enrollments.exists():
            return ("skip_already_enrolled", "primary already has an enrollment")

        case = internal_service_case(primary)
        if case is None:
            return ("skip_no_internal_case", "primary has no internal-service case")

        try:
            total = int(float(cells.get(_COL_TOTAL) or 1))
        except ValueError:
            total = 1

        # The case import auto-creates a solo household per client
        # (ensure_household_with_primary on internal-service cases), so the
        # primary almost always already has one. Reuse it; only skip when the
        # primary actually belongs to ANOTHER family (a dependent elsewhere).
        primary_membership = (
            HouseholdMember.objects.filter(client=primary)
            .select_related("household")
            .first()
        )
        if primary_membership is not None and not primary_membership.is_primary:
            return ("skip_member_of_other_household", "primary is a dependent in another household")
        if primary_id in listed_as_member:
            return ("skip_member_of_other_household", "listed as a member in another row")

        household_name = f"{(primary.last_name or '').strip()} Household".strip()
        if primary_membership is not None:
            household = primary_membership.household
            if not household.name:
                household.name = household_name
                household.save(update_fields=["name"])
        else:
            household = Household.objects.create(name=household_name)
            HouseholdMember.objects.create(
                household=household, client=primary, is_primary=True
            )

        # --- Fold dependents into the primary's household ---
        block_for = {primary_id: _MEMBER_BLOCKS[0]}
        member_clients = [primary]
        unresolved = 0
        in_other_household = 0
        for block in _MEMBER_BLOCKS[1:]:
            mid = _clean(cells.get(block[0]))
            if not mid or mid == primary_id or mid in block_for:
                continue
            c = Client.objects.filter(client_id=mid).first()
            if c is None:
                unresolved += 1
                continue
            existing = (
                HouseholdMember.objects.filter(client=c)
                .select_related("household")
                .first()
            )
            if existing is not None and existing.household_id == household.household_id:
                pass  # already a member of this household
            elif existing is not None:
                old = existing.household
                # Merge a dependent's auto-created SOLO household into this
                # family; never pull a member out of another real household.
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
                    in_other_household += 1
                    continue
            else:
                HouseholdMember.objects.create(
                    household=household, client=c, is_primary=False
                )
            member_clients.append(c)
            block_for[str(c.client_id)] = block

        # --- Delivery address (on the primary) ---
        address = self._delivery_address(primary, cells)

        # --- Enrollment ---
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

        # --- Member dietary profiles ---
        primary_kind = None
        for m in member_clients:
            block = block_for.get(str(m.client_id), _MEMBER_BLOCKS[0])
            fields, kind = _profile_fields(block, cells)
            if m is primary:
                primary_kind = kind
            MemberDietaryProfile.objects.create(
                enrollment=enr,
                client=m,
                member_name=f"{m.first_name or ''} {m.last_name or ''}".strip(),
                **fields,
            )

        advance_enrollment(
            enr, EnrollmentStage.VERIFIED, force=True,
            note="Imported from Meal Inputs verification sheet.",
        )

        # --- Kitchen assignment + activation (only when authorized) ---
        facility = (cells.get(_COL_FACILITY) or "").strip().lower()
        kitchen = kitchens.get(_FACILITY_TO_KITCHEN.get(facility, ""))
        is_boxes = primary_kind == ProductTypeKind.BOXES
        cadence_code = (cells.get(_COL_CADENCE) or "").strip().lower()
        meal_cadence = cadence_map.get(cadence_code)
        authorized = case.service_authorization_status in _AUTHORIZED
        has_cadence = is_boxes or meal_cadence is not None

        if not authorized:
            return ("verified_only", "case not authorized")
        if kitchen is None:
            return ("verified_only", "no kitchen (blank/unmapped facility)")
        if not has_cadence:
            return ("verified_only", "meals row without a cadence")

        enr.kitchen = kitchen
        enr.save(update_fields=["kitchen"])
        for profile in enr.member_profiles.all():
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
        return ("activated",)

    def _delivery_address(self, primary, cells):
        street = _clean(cells.get(_COL_STREET))
        city = _clean(cells.get(_COL_CITY))
        if not (street or city):
            return None
        now = timezone.now()
        return Address.objects.create(
            client=primary,
            type=AddressType.DELIVERY,
            street=street[:255],
            city=city[:120],
            state=_clean(cells.get(_COL_STATE))[:2],
            zip=_clean(cells.get(_COL_ZIP))[:10],
            created_at=now,
            updated_at=now,
        )

    def _report(self, report, verified_reasons, flags, apply, cadence_a):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Meal verification import ==="))
        self.stdout.write(f"Cadence 'A' -> {cadence_a} ('B' -> the other)")
        order = [
            ("activated", "Activated (kitchen + service active)"),
            ("verified_only", "Built + verified (not activated)"),
            ("skip_already_enrolled", "Skipped: already enrolled"),
            ("skip_no_internal_case", "Skipped: no internal-service case"),
            ("skip_member_of_other_household", "Skipped: single, already in a household"),
            ("skip_primary_in_household", "Skipped: primary already in a household"),
            ("skip_primary_not_found", "Skipped: primary id not found"),
            ("skip_no_primary_id", "Skipped: blank primary id"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<44}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<44}: {sum(report.values())}")

        if verified_reasons:
            self.stdout.write(head("\nVerified-only (not activated) by reason:"))
            for reason, n in verified_reasons.most_common():
                self.stdout.write(f"  {reason:<44}: {n}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 25):"))
            for pid, reason in flags[:25]:
                self.stdout.write(f"  {pid or '(blank)'}: {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

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
  * Cadence/Facility: the sheet value wins; when blank we fall back to the Cadence
    CSV (``--cadence-csv``, keyed by client id). Rows still lacking a kitchen stay
    verified-only (not activated).
  * Delivery address: taken from the sheet; when blank it falls back to the
    client's primary (current/home/mailing) address, copied into a DELIVERY record.

Column mapping:
  A=primary id, B-E=address, F=address notes, I/J/K/L=primary meal cat / allergies /
  other-allergy / other-restrictions, M=general note, N=total members,
  HM#2..#10 in 5-col blocks (id, meal category, food allergies, other allergies,
  other restrictions), BH=Cadence (A->Mon/Thu, B->Tue/Fri, Boxes), BI=Facility
  (ENG, AST, Boxes->Hicksville).

Usage:
    python manage.py import_meal_verifications                 # DRY RUN (rolls back)
    python manage.py import_meal_verifications --apply          # commit
    python manage.py import_meal_verifications --limit 50       # first 50 rows
    python manage.py import_meal_verifications --file path.xlsx
"""
import csv
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
# Fallback source for Cadence/Facility when the sheet leaves them blank: a CSV
# keyed by Unite Us Client ID with `Cadence` (A/B/Boxes) and `Facility`
# (ENG/AST/Hicksville) columns.
_DEFAULT_CADENCE_CSV = "tmp/verification/Cadence.csv"

# Columns are resolved by HEADER LABEL, not fixed position: the "Meal Inputs"
# export has shifted columns between versions (e.g. LIST8 inserted an
# "Address-Apt" column and carries an "HM #1" block instead of "HM #10").
_H_PRIMARY_ID = "Unite Us Client ID"
_H_STREET = "Address - Street"
_H_APT = "Address-Apt"
_H_CITY = "Address - City"
_H_STATE = "Address - State"
_H_ZIP = "Address - Postal Code"
_H_ADDR_NOTES = "Address Notes"
_H_TOTAL = "Total Household Members"
_H_CADENCE = "Cadence"
_H_FACILITY = "Facility"

# The primary member's diet columns are labelled differently from the HM blocks.
_PRIMARY_BLOCK_NAMES = (
    _H_PRIMARY_ID, "Meal Category (Input)", "Allergy Note (Input)",
    "Other Allergy Note", "Other Restrictions",
)
# Max HM #k block index to probe; only blocks whose ID column is present are used.
_MAX_HM = 12


def _hm_block_names(k):
    """Header labels for the HM #k block: (id, meal category, food allergies,
    other allergies, other restrictions)."""
    return (
        f"HM #{k} - Enrollment Platform Client ID",
        f"HM #{k} - Meal Category",
        f"HM #{k} - Food Allergies",
        f"HM #{k} - Other Allergies",
        f"HM #{k} - Other Restrictions",
    )

# Sheet Facility code -> Kitchen.name.
_FACILITY_TO_KITCHEN = {"eng": "ENG", "ast": "AST", "boxes": "Hicksville", "hicksville": "Hicksville"}
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


def _read_sheet(path):
    """Parse the first worksheet. Returns ``(name_to_col, rows)``:

    * ``name_to_col`` -- ``{header label -> column letter}`` from row 1.
    * ``rows`` -- list of ``{column_letter: value}`` for the data rows.

    Pure stdlib so no openpyxl/pandas dependency."""
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
    name_to_col, rows = {}, []
    for r in ws.iter(_NS + "row"):
        cells = {}
        for c in r.findall(_NS + "c"):
            v = c.find(_NS + "v")
            if v is None:
                continue
            val = shared[int(v.text)] if c.get("t") == "s" else v.text
            cells[_col(c.get("r"))] = (val or "").strip()
        if r.get("r") == "1":
            for letter, val in cells.items():
                if val:
                    name_to_col[val] = letter
            continue
        if cells:
            rows.append(cells)
    return name_to_col, rows


def _read_rows(path):
    """Back-compat helper (used by other commands): just the data rows."""
    return _read_sheet(path)[1]


def _load_cadence_csv(path):
    """Parse the Cadence CSV into ``{client_id_lower: (cadence, facility)}``.

    Returns ``{}`` (and never raises) when the path is blank or missing so the
    importer still runs on sheets that carry their own Cadence/Facility."""
    mapping = {}
    if not path:
        return mapping
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if not row:
                    continue
                cid = (row[0] or "").strip().lower()
                cadence = (row[1] if len(row) > 1 else "").strip()
                facility = (row[2] if len(row) > 2 else "").strip()
                if cid:
                    mapping[cid] = (cadence, facility)
    except FileNotFoundError:
        return mapping
    return mapping


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _clean(value):
    v = (value or "").strip().strip('"').strip("\u201c\u201d").strip()
    return "" if v.lower() in _BLANKS else v


def _client_id(value):
    """Return a cleaned value only if it is a well-formed UUID, else ''.

    Sheets occasionally carry malformed ids (smart quotes, a trailing
    ``/cases`` fragment, etc.); those must be skipped, not crash the row."""
    v = _clean(value)
    return v if _UUID_RE.match(v) else ""


def _split_tokens(value):
    return [t.strip().strip('"') for t in re.split(r"[;,]", value or "") if t.strip()]


def _parse_meal_category(raw):
    """'132 - Fresh (Meals)' -> ('Standard', 'fresh_meal', ProductTypeKind.MEALS).

    Newer sheets carry categories WITHOUT a ``(Meals)/(Boxes)`` suffix (e.g.
    ``Standard/Fresh``, bare ``Kosher``/``Halal``/``Dairy Free``). When a menu is
    recognised but no product kind is stated, default to MEALS (boxes always say
    so explicitly)."""
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
    # "standard" alone (no "fresh") still means the Standard menu.
    if not menu and "standard" in low:
        menu = "Standard"
    if kind is None and menu:
        kind = ProductTypeKind.MEALS
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
        parser.add_argument(
            "--cadence-csv",
            default=_DEFAULT_CADENCE_CSV,
            help="CSV (Client ID, Cadence, Facility) used to fill Cadence/Facility "
            "when the sheet leaves them blank. Pass '' to disable.",
        )
        parser.add_argument("--limit", type=int, default=0, help="Process first N rows.")
        parser.add_argument(
            "--cadence-a",
            choices=["mon_thu", "tue_fri"],
            default="mon_thu",
            help="Which meal cadence sheet code 'A' maps to (B gets the other).",
        )
        # Defaults for thin sheets (e.g. the Williamsburg list) whose Facility /
        # Cadence / Meal Category columns are blank. Only fill gaps -- a value
        # present in the row always wins.
        parser.add_argument(
            "--default-facility",
            default="",
            help="Kitchen name to use when a row's Facility is blank/unmapped "
            "(e.g. 'Williamsburg').",
        )
        parser.add_argument(
            "--default-cadence",
            choices=["mon_thu", "tue_fri"],
            default="",
            help="Meal cadence to use when a row's Cadence is blank.",
        )
        parser.add_argument(
            "--default-menu",
            default="",
            help="MenuType name to use when a member's meal category is blank "
            "(implies the member is a meals member, e.g. 'Kosher').",
        )

    def _build_columns(self, name2col):
        """Resolve the columns we read by their HEADER LABEL (positions vary
        between sheet versions). Sets the address/total/cadence/facility column
        letters and the ordered member blocks (primary first, then every HM #k
        block present)."""
        def L(name):
            return name2col.get(name)

        self.col_street = L(_H_STREET)
        self.col_apt = L(_H_APT)
        self.col_city = L(_H_CITY)
        self.col_state = L(_H_STATE)
        self.col_zip = L(_H_ZIP)
        self.col_addr_notes = L(_H_ADDR_NOTES)
        self.col_total = L(_H_TOTAL)
        self.col_cadence = L(_H_CADENCE)
        self.col_facility = L(_H_FACILITY)

        primary = tuple(L(n) for n in _PRIMARY_BLOCK_NAMES)
        if primary[0] is None:
            raise CommandError(
                f"Sheet is missing the required '{_H_PRIMARY_ID}' column."
            )
        hm = []
        for k in range(1, _MAX_HM + 1):
            names = _hm_block_names(k)
            if L(names[0]) is None:
                continue  # this HM block isn't present in the sheet
            hm.append(tuple(L(n) for n in names))
        self.member_blocks = [primary] + hm
        self.hm_blocks = hm

    def handle(self, *args, **options):
        apply = options["apply"]
        name2col, rows = _read_sheet(options["file"])
        self._build_columns(name2col)
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

        # Gap-fill defaults for thin sheets (blank facility/cadence/menu).
        self.default_facility = (options.get("default_facility") or "").strip()
        self.default_menu = (options.get("default_menu") or "").strip()
        dc = options.get("default_cadence") or ""
        self.default_cadence = (
            DeliveryCadence.MON_THU
            if dc == "mon_thu"
            else DeliveryCadence.TUE_FRI
            if dc == "tue_fri"
            else None
        )

        # Cadence/Facility fallback keyed by client id (blank sheet cells only).
        cadence_csv = _load_cadence_csv(options.get("cadence_csv") or "")

        kitchens = {k.name: k for k in Kitchen.objects.all()}

        # Pass 1: every client id listed as a household MEMBER (any HM block) in
        # any row — used to skip their own single-member row (they belong to a
        # household).
        listed_as_member = set()
        for cells in rows:
            for block in self.hm_blocks:
                mid = _client_id(cells.get(block[0]))
                if mid:
                    listed_as_member.add(mid)

        report = Counter()
        verified_reasons = Counter()
        flags = []  # (primary_id, reason)

        with transaction.atomic():
            for cells in rows:
                primary_id = _client_id(cells.get("A"))
                try:
                    with transaction.atomic():
                        outcome = self._process_row(
                            cells, primary_id, kitchens, cadence_map,
                            listed_as_member, cadence_csv,
                        )
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                reason = outcome[1] if len(outcome) > 1 else ""
                if outcome[0] in ("kitchen_assignment", "needs_verification"):
                    verified_reasons[f"{outcome[0]}: {reason}"] += 1
                elif outcome[0] != "activated":
                    flags.append((primary_id, reason))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, verified_reasons, flags, apply, options["cadence_a"])

    def _process_row(self, cells, primary_id, kitchens, cadence_map,
                     listed_as_member, cadence_csv):
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
            total = int(float(cells.get(self.col_total) or 1))
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
        block_for = {primary_id: self.member_blocks[0]}
        member_clients = [primary]
        unresolved = 0
        in_other_household = 0
        for block in self.member_blocks[1:]:
            mid = _client_id(cells.get(block[0]))
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
        primary_menu_type = ""
        for m in member_clients:
            block = block_for.get(str(m.client_id), self.member_blocks[0])
            fields, kind = _profile_fields(block, cells)
            if self.default_menu and not fields["menu_type"]:
                fields["menu_type"] = self.default_menu
            if m is primary:
                primary_kind = kind
                primary_menu_type = fields["menu_type"]
            MemberDietaryProfile.objects.create(
                enrollment=enr,
                client=m,
                member_name=f"{m.first_name or ''} {m.last_name or ''}".strip(),
                **fields,
            )

        # A blank primary meal category with a default menu -> treat as meals.
        if primary_kind is None and self.default_menu:
            primary_kind = ProductTypeKind.MEALS

        # --- Resolve kitchen + cadence (sheet wins; Cadence CSV fills blanks) ---
        csv_cadence, csv_facility = cadence_csv.get((primary_id or "").lower(), ("", ""))
        facility = (cells.get(self.col_facility) or "").strip().lower() \
            or (csv_facility or "").strip().lower()
        kitchen = kitchens.get(_FACILITY_TO_KITCHEN.get(facility, ""))
        if kitchen is None and self.default_facility:
            kitchen = kitchens.get(self.default_facility)
        is_boxes = primary_kind == ProductTypeKind.BOXES
        cadence_code = (cells.get(self.col_cadence) or "").strip().lower() \
            or (csv_cadence or "").strip().lower()
        meal_cadence = cadence_map.get(cadence_code)
        if meal_cadence is None and not is_boxes and self.default_cadence:
            meal_cadence = self.default_cadence

        # --- Tiered outcome by data completeness (auth gates ACTIVE only) ---
        has_address = address is not None
        has_menu = bool(primary_menu_type)
        has_cadence = is_boxes or meal_cadence is not None
        authorized = case.service_authorization_status in _AUTHORIZED

        # Tier 3: missing delivery address OR menu type -> stays Pending
        # Verification (enrollment already created at that stage; do not advance).
        if not has_address or not has_menu:
            missing = []
            if not has_address:
                missing.append("delivery address")
            if not has_menu:
                missing.append("menu type")
            return ("needs_verification", "missing " + " + ".join(missing))

        # Address + menu type present -> the household is Verified.
        advance_enrollment(
            enr, EnrollmentStage.VERIFIED, force=True,
            note="Imported from Meal Inputs verification sheet.",
        )

        # Tier 1: data-complete (kitchen + cadence) AND authorized -> activate.
        if kitchen is not None and has_cadence and authorized:
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

        # Tier 2: verified but can't activate yet -> Kitchen Assignment.
        if not authorized:
            reason = "case not authorized"
        elif kitchen is None:
            reason = "no kitchen (blank/unmapped facility)"
        else:
            reason = "meals row without a cadence"
        advance_enrollment(
            enr, EnrollmentStage.KITCHEN_ASSIGNMENT, force=True,
            note=f"Imported; verified, awaiting kitchen assignment ({reason}).",
        )
        return ("kitchen_assignment", reason)

    def _delivery_address(self, primary, cells):
        street = _clean(cells.get(self.col_street))
        city = _clean(cells.get(self.col_city))
        now = timezone.now()
        if street or city:
            return Address.objects.create(
                client=primary,
                type=AddressType.DELIVERY,
                street=street[:255],
                unit=_clean(cells.get(self.col_apt))[:60] if self.col_apt else "",
                city=city[:120],
                state=_clean(cells.get(self.col_state))[:2],
                zip=_clean(cells.get(self.col_zip))[:10],
                notes=_clean(cells.get(self.col_addr_notes)),
                created_at=now,
                updated_at=now,
            )
        # No delivery address on the sheet -> fall back to the client's primary
        # (current/home) address, copied into a DELIVERY record.
        src = self._primary_address(primary)
        if src is None:
            return None
        return Address.objects.create(
            client=primary,
            type=AddressType.DELIVERY,
            street=src.street,
            unit=src.unit,
            city=src.city,
            county=src.county,
            state=src.state,
            zip=src.zip,
            notes=src.notes,
            created_at=now,
            updated_at=now,
        )

    # Address types (in priority order) that represent a client's "primary"
    # residence we can deliver to when the sheet has no delivery address.
    _PRIMARY_ADDRESS_TYPES = (
        AddressType.CURRENT,
        AddressType.HOME,
        AddressType.MAILING,
    )

    def _primary_address(self, client):
        """The client's best existing non-delivery address (with a street/city)."""
        addrs = [
            a for a in client.addresses.all()
            if (a.street or a.city) and a.type != AddressType.DELIVERY
        ]
        if not addrs:
            return None
        for kind in self._PRIMARY_ADDRESS_TYPES:
            for a in addrs:
                if a.type == kind:
                    return a
        return addrs[0]

    def _report(self, report, verified_reasons, flags, apply, cadence_a):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Meal verification import ==="))
        self.stdout.write(f"Cadence 'A' -> {cadence_a} ('B' -> the other)")
        order = [
            ("activated", "Service Active (verified + kitchen + activated)"),
            ("kitchen_assignment", "Verified -> Kitchen Assignment (awaiting kitchen)"),
            ("needs_verification", "Pending Verification (missing address/menu)"),
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
            self.stdout.write(head("\nKitchen-assignment / pending-verification by reason:"))
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

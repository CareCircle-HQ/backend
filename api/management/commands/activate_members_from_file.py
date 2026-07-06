"""One-off: activate the "Active Members" roster (ActiveMembers.xlsx) straight
to Service Active, with every member's required info set up from the sheet.

The sheet is a FLAT per-client list carrying everything needed to put a member
into service:

    | Unite Us Client ID | Address - Street | Address-Apt | Address - City |
      Address - State | Address - Postal Code | Address Notes |
      Meal Category (Input) | Allergy Note (Input) | Other Allergy Note |
      Other Restrictions | General Verification Note | Cadence | Facility |

Per row it (only for clients NOT already enrolled):

  * creates the delivery Address from the sheet,
  * builds the household (client as primary),
  * creates a verification enrollment + one MemberDietaryProfile with the menu
    type (Meal Category), parsed food allergies, restrictions and notes,
  * assigns the kitchen (Facility) and cadence (Cadence),
  * FORCES the member ACTIVE -- this roster must never produce an Out of Orbit
    or Paused member (those are handled via a separate list). The meal rule is
    still evaluated for REPORTING only: rows the rule *would* have sent Out of
    Orbit are counted + listed so they can go on that separate list.
  * builds the delivery schedule + dated calendar and advances to SERVICE_ACTIVE.

Required info is validated up-front. A row missing a mappable kitchen, menu
type, cadence, or a complete address is NOT activated and is reported under
"cannot activate" so nothing goes into service half-configured.

Idempotent: an already-enrolled client is never re-activated (reported only).
Dry-run unless --apply; --force is required to COMMIT when warnings exist
(already-enrolled clients found Out of Orbit / Paused / On Hold).

Cadence map (confirmed from the roster<->facility correlation + box rules):
    A     -> Mon/Thu (meals)
    B     -> Tue/Fri (meals)
    Boxes -> box product (fixed Wednesday delivery, PO cut the Friday before)

Usage:
    python manage.py activate_members_from_file                     # dry run
    python manage.py activate_members_from_file --apply --force      # commit
    python manage.py activate_members_from_file --file other.xlsx
"""
import re
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    Address,
    Client,
    DeliveryCadence,
    DietaryRestriction,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MemberStatus,
    MenuCategory,
    MenuType,
    ProductTypeKind,
)
from api.portal.serializers import internal_service_case
from api.serializers import ensure_household_with_primary
from api.services.delivery import create_member_delivery_schedules
from api.services.lifecycle import advance_enrollment
from api.services.meal_rules import _real_allergies, resolve_kitchen_meal
from api.services.orders import generate_delivery_calendar

_DEFAULT_FILE = "tmp/verification/ActiveMembers.xlsx .xlsx"

# Column headers (read by NAME so column order can't break it).
_COL = {
    "id": "Unite Us Client ID",
    "street": "Address - Street (AI Cleaned)",
    "apt": "Address-Apt (AI Cleaned)",
    "city": "Address - City",
    "state": "Address - State",
    "zip": "Address - Postal Code",
    "addr_notes": "Address Notes",
    "meal": "Meal Category (Input)",
    "allergy": "Allergy Note (Input)",
    "other_allergy": "Other Allergy Note",
    "other_restr": "Other Restrictions",
    "verif_note": "General Verification Note",
    "cadence": "Cadence",
    "facility": "Facility",
}

# Cadence code -> (DeliveryCadence, delivery weekday codes). "Boxes" is handled
# separately (box product: fixed Wednesday, ignores the cadence weekday).
_CADENCE_MAP = {
    "A": (DeliveryCadence.MON_THU, ["mon", "thu"]),
    "B": (DeliveryCadence.TUE_FRI, ["tue", "fri"]),
}
_BOXES_CODE = "Boxes"

# Meal Category (sheet) that isn't a catalog MenuType -> the menu to store.
# "Allergen Free" isn't a client menu; the meal rule derives the allergen-free
# KITCHEN output from the allergies, so the client menu is Standard.
_MENU_ALIAS = {"allergen free": "standard"}

# Menu name -> MenuCategory (coarse product category on the profile).
_MENU_TO_CATEGORY = {
    "dairy free": MenuCategory.DAIRY_FREE,
    "fish free": MenuCategory.FISH_FREE,
    "vegetarian": MenuCategory.VEGETARIAN,
}

# Allergy token (normalized) -> FoodAllergy code, for tokens that don't already
# equal a code after "X free"/spacing normalization.
_ALLERGY_ALIAS = {
    "tree_nuts": "tree_nuts",
    "treenuts": "tree_nuts",
    "tree_nut": "tree_nuts",
    "red_meat": "red_meat",
    "peanut": "peanuts",
    "egg": "eggs",
    "others": "other",
}
_NON_ALLERGY = {"", "none", "0", "na", "n/a", "no", "0.0"}
_FOOD_CODES = {c for c, _ in FoodAllergy.choices}
_FOOD_LABELS = dict(FoodAllergy.choices)

# "Other Restrictions" token (normalized) -> DietaryRestriction code.
_RESTRICTION_ALIAS = {
    "diabetic": "diabetes",
    "diabetes": "diabetes",
    "cardiometabolic": "cardio_metabolic",
    "cardio_metabolic": "cardio_metabolic",
    "cardio-metabolic": "cardio_metabolic",
    "postpartum": "postpartum",
    "post_partum": "postpartum",
}
_NON_RESTRICTION = {"", "none", "no restrictions", "no restriction", "0", "na", "n/a"}
_RESTRICTION_CODES = {c for c, _ in DietaryRestriction.choices}


def _norm(value):
    return "" if value is None else str(value).strip()


def _read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    header = [_norm(c) for c in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    col = {key: idx.get(name) for key, name in _COL.items()}
    out = []
    for r in rows[1:]:
        rec = {}
        for key, i in col.items():
            rec[key] = _norm(r[i]) if i is not None and i < len(r) else ""
        if rec["id"]:
            rec["id"] = rec["id"].lower()
            out.append(rec)
    return out


def parse_allergies(raw):
    """(codes, unknown_tokens). Empty/none/0 -> ['none']."""
    if not raw:
        return ["none"], []
    codes, unknown = [], []
    for tok in re.split(r"[,/;]", raw):
        t = tok.strip().strip('"').strip("'").strip().lower()
        if t in _NON_ALLERGY:
            continue
        if t.endswith(" free"):  # "Pork Free" note -> the pork allergy itself
            t = t[:-5].strip()
        key = t.replace(" ", "_")
        code = _ALLERGY_ALIAS.get(key, key)
        if code in _FOOD_CODES:
            codes.append(code)
        else:
            unknown.append(tok.strip())
    if not codes:
        return (["none"] if not unknown else []), unknown
    # de-dupe, preserve order
    return list(dict.fromkeys(codes)), unknown


def parse_restrictions(raw):
    """(codes, unknown_tokens) from the 'Other Restrictions' column. Empty /
    'No Restrictions' / 'None' -> ['none']."""
    if not raw:
        return ["none"], []
    codes, unknown = [], []
    for tok in re.split(r"[,/;]", raw):
        t = tok.strip().strip('"').strip("'").strip().lower()
        if t in _NON_RESTRICTION:
            continue
        code = _RESTRICTION_ALIAS.get(t.replace(" ", "_"), t.replace(" ", "_"))
        if code in _RESTRICTION_CODES and code != "none":
            codes.append(code)
        else:
            unknown.append(tok.strip())
    if not codes:
        return (["none"] if not unknown else []), unknown
    return list(dict.fromkeys(codes)), unknown


class Command(BaseCommand):
    help = (
        "Activate the ActiveMembers roster to Service Active (force-active, no "
        "Out of Orbit/Paused). Dry-run unless --apply; --force to commit past "
        "warnings."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Roster .xlsx path.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--force", action="store_true",
            help="Required with --apply to COMMIT when warnings exist.",
        )

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]
        force = options["force"]

        rows = _read_rows(path)
        if not rows:
            self.stdout.write(self.style.ERROR(f"No rows read from {path}."))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Active Members roster: {path} -> {len(rows)} client rows"
        ))

        # Lookups.
        self.kitchens = {k.name.strip().lower(): k for k in Kitchen.objects.all()}
        self.menus = {m.name.strip().lower(): m.name for m in MenuType.objects.all()}

        report = Counter()
        self.would_ooo = []       # (id, menu, allergies) - forced active anyway
        self.unknown_allergies = Counter()
        self.unknown_restrictions = Counter()
        self.cannot = []          # (id, reason)
        self.warn = {"on_hold": [], "out_of_orbit": [], "paused": []}
        self.blocked = False

        with transaction.atomic():
            for rec in rows:
                try:
                    with transaction.atomic():
                        bucket = self._process(rec)
                except Exception as exc:  # isolate a bad row
                    bucket = "error"
                    self.cannot.append((rec["id"], f"error: {exc}"))
                report[bucket] += 1

            has_warnings = any(self.warn.values())
            if not apply:
                transaction.set_rollback(True)
            elif has_warnings and not force:
                transaction.set_rollback(True)
                self.blocked = True

        self._report(report, apply, force, len(rows))

    # -- per-row -----------------------------------------------------------
    def _process(self, rec):
        cid = rec["id"]
        client = Client.objects.filter(client_id=cid).first()
        if client is None:
            self.cannot.append((cid, "client id not in DB"))
            return "missing"

        membership = HouseholdMember.objects.filter(client=client).first()
        if membership is not None and not membership.is_primary:
            self.cannot.append((cid, "dependent in another household"))
            return "dependent"

        enr = client.enrollments.order_by("-opened_at").first()
        if enr is not None:
            self._collect_warnings(client, enr)
            return "already_enrolled"

        # Validate required info BEFORE activating anything.
        kitchen = self.kitchens.get(rec["facility"].strip().lower())
        if kitchen is None:
            self.cannot.append((cid, f"unknown facility {rec['facility']!r}"))
            return "cannot_activate"

        menu = self._resolve_menu(rec["meal"])
        if menu is None:
            self.cannot.append((cid, f"unknown meal category {rec['meal']!r}"))
            return "cannot_activate"

        cad = rec["cadence"].strip()
        is_boxes = cad == _BOXES_CODE
        if not is_boxes and cad.upper() not in _CADENCE_MAP:
            self.cannot.append((cid, f"unknown cadence {cad!r}"))
            return "cannot_activate"

        missing_addr = [
            f for f in ("street", "city", "state", "zip") if not rec[f]
        ]
        if missing_addr:
            self.cannot.append((cid, f"incomplete address (missing {', '.join(missing_addr)})"))
            return "cannot_activate"

        self._activate(client, rec, kitchen, menu, is_boxes, cad)
        return "activated"

    def _resolve_menu(self, meal_category):
        key = meal_category.strip().lower()
        key = _MENU_ALIAS.get(key, key)
        name = self.menus.get(key)
        return name

    def _activate(self, client, rec, kitchen, menu, is_boxes, cad):
        # Food allergies + dietary restrictions + free-text notes from the sheet.
        allergies, unknown_al = parse_allergies(rec["allergy"])
        restrictions, unknown_re = parse_restrictions(rec["other_restr"])
        for u in unknown_al:
            self.unknown_allergies[u] += 1
        for u in unknown_re:
            self.unknown_restrictions[u] += 1
        # Free-text notes: the "Other Allergy Note" free-text items (Beans,
        # Vegan, No fried foods, ...) plus any allergy/restriction tokens that
        # didn't map to a DB code, so no information is lost.
        extra_notes = " | ".join(
            x for x in ([rec["other_allergy"]] + unknown_al + unknown_re) if x
        )

        # Force-active kitchen output: use the meal rule when it can fulfill the
        # member; otherwise FORCE active and carry the menu + allergies as
        # kitchen notes (never Out of Orbit for this roster).
        result = resolve_kitchen_meal(menu, allergies)
        if result.out_of_orbit:
            self.would_ooo.append((rec["id"], menu, ",".join(a for a in allergies if a != "none")))
            real = _real_allergies(allergies)
            kitchen_meal_type = menu
            kitchen_food_notes = ", ".join(
                f"{_FOOD_LABELS.get(a, a)} Free" for a in sorted(real)
            )
        else:
            kitchen_meal_type = result.kitchen_meal_type
            kitchen_food_notes = result.kitchen_food_notes

        # Delivery address from the sheet.
        address = Address.objects.create(
            client=client, type="temporary",
            street=rec["street"], unit=rec["apt"], city=rec["city"],
            state=rec["state"], zip=rec["zip"], notes=rec["addr_notes"],
        )

        household = ensure_household_with_primary(client)
        case = internal_service_case(client)
        program = case.program if (case and case.program_id) else None

        if is_boxes:
            cadence_val, weekdays, product_kind = "", ["wed"], ProductTypeKind.BOXES
        else:
            cadence_val, weekdays = _CADENCE_MAP[cad.upper()]
            product_kind = None

        enr = EnrollmentVerification.objects.create(
            client=client,
            household=household,
            case=case,
            program_name=(program.name if program else "")
            or (case.program_name if case else ""),
            service_type=(case.service_type if case else "") or "",
            delivery_address=address,
            delivery_weekdays=weekdays,
            household_size=household.members.count(),
            is_family_verified=True,
            medicaid_type_verified=True,
            delivery_address_verified=True,
            verified_at=timezone.now(),
            kitchen=kitchen,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )

        MemberDietaryProfile.objects.create(
            enrollment=enr,
            client=client,
            member_name=f"{client.first_name} {client.last_name}".strip(),
            menu_type=menu,
            food_allergies=allergies,
            dietary_restrictions=restrictions,
            other_dietary_restrictions=extra_notes,
            meal_category=_MENU_TO_CATEGORY.get(menu.lower(), MenuCategory.FRESH_MEAL),
            status=MemberStatus.ACTIVE,
            kitchen_meal_type=kitchen_meal_type,
            kitchen_food_notes=kitchen_food_notes,
            general_verification_notes=rec["verif_note"],
        )

        advance_enrollment(
            enr, EnrollmentStage.VERIFIED, force=True,
            note="Active Members import: auto-verified.",
        )
        create_member_delivery_schedules(
            enr, case=case, cadence=cadence_val, kitchen=kitchen,
            product_kind=product_kind,
        )
        generate_delivery_calendar(enr)
        advance_enrollment(
            enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
            note=f"Active Members import: activated ({kitchen.name}).",
        )

    def _collect_warnings(self, client, enr):
        cid = str(client.client_id)
        if enr.stage == EnrollmentStage.ON_HOLD:
            self.warn["on_hold"].append(cid)
        for mp in enr.member_profiles.all():
            if mp.status == MemberStatus.OUT_OF_ORBIT:
                self.warn["out_of_orbit"].append(f"{cid} ({mp.member_name})")
            elif mp.status == MemberStatus.PAUSED:
                self.warn["paused"].append(f"{cid} ({mp.member_name})")

    # -- report ------------------------------------------------------------
    def _report(self, report, apply, force, total):
        head = self.style.MIGRATE_HEADING

        activated = report.get("activated", 0)
        already = report.get("already_enrolled", 0)
        missing = report.get("missing", 0)
        cannot = report.get("cannot_activate", 0)
        dependent = report.get("dependent", 0)
        errored = report.get("error", 0)
        not_active = missing + cannot + dependent + errored

        self.stdout.write(head("\n=== Will become ACTIVE vs NOT ==="))
        self.stdout.write(self.style.SUCCESS(
            f"  ACTIVE (new activations)                     : {activated}"
        ))
        self.stdout.write(
            f"  (already enrolled -> left as-is)             : {already}"
        )
        self.stdout.write(self.style.WARNING(
            f"  NOT ACTIVE (cannot process)                  : {not_active}"
        ))
        self.stdout.write(f"      - missing from DB                        : {missing}")
        self.stdout.write(f"      - incomplete/unmappable info             : {cannot}")
        if dependent:
            self.stdout.write(f"      - dependent in another household         : {dependent}")
        if errored:
            self.stdout.write(f"      - errored                                : {errored}")
        self.stdout.write(f"  {'TOTAL rows':<44}: {total}")

        # Separate-list report: rows FORCED active that the meal rule would have
        # sent Out of Orbit (these need the separate handling).
        if self.would_ooo:
            self.stdout.write(head(
                f"\nForced ACTIVE but meal-rule would flag Out of Orbit "
                f"({len(self.would_ooo)}) -- for the separate list:"
            ))
            for cid, menu, al in self.would_ooo[:60]:
                self.stdout.write(f"  {cid}: menu={menu} allergies={al or '-'}")
            if len(self.would_ooo) > 60:
                self.stdout.write(f"  ... (+{len(self.would_ooo) - 60} more)")

        if self.unknown_allergies:
            self.stdout.write(head("\nUnrecognized allergy tokens (stored as notes):"))
            for tok, n in self.unknown_allergies.most_common():
                self.stdout.write(f"  {tok!r}: {n}")

        if self.unknown_restrictions:
            self.stdout.write(head("\nUnrecognized restriction tokens (stored as notes):"))
            for tok, n in self.unknown_restrictions.most_common():
                self.stdout.write(f"  {tok!r}: {n}")

        if self.cannot:
            self.stdout.write(head(f"\nCannot activate ({len(self.cannot)}, up to 60):"))
            for cid, reason in self.cannot[:60]:
                self.stdout.write(f"  {cid}: {reason}")
            if len(self.cannot) > 60:
                self.stdout.write(f"  ... (+{len(self.cannot) - 60} more)")

        # Warnings: already-enrolled clients found not cleanly active.
        total_warn = sum(len(v) for v in self.warn.values())
        if total_warn:
            self.stdout.write(self.style.ERROR(
                f"\n!!! WARNING: {total_warn} already-enrolled client(s) are NOT "
                "cleanly active (handle via the separate list) !!!"
            ))
            for key, label in (
                ("on_hold", "On Hold"),
                ("out_of_orbit", "Out of Orbit member"),
                ("paused", "Paused member"),
            ):
                if self.warn[key]:
                    self.stdout.write(self.style.WARNING(f"  {label} ({len(self.warn[key])}):"))
                    for x in self.warn[key][:40]:
                        self.stdout.write(f"      {x}")

        if self.blocked:
            self.stdout.write(self.style.ERROR(
                "\nNOT APPLIED: rolled back because warnings exist. Review above, "
                "then re-run with --apply --force to commit."
            ))
        elif apply:
            self.stdout.write(self.style.SUCCESS(
                "\nAPPLIED (committed)" + (" [--force]" if force else "") + "."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

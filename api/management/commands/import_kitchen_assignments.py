"""Import the flat "Kitchen Assignment List with Cadence Facility" CSV.

Unlike ``import_meal_verifications`` (one row = one household, keyed by the
primary), this sheet is FLAT: one row = one client, already resolved to a
delivery address, menu type, food notes, allergies, cadence and facility. Every
client in the file is the primary of their own household.

For each ``client_id`` the command:
  * splits the single-string ``delivery_address`` into street / apt / city /
    state / zip (ZIP is authoritative for the state -- the sheet has "Ne"
    typos for NY),
  * maps ``facility`` -> Kitchen (ENG/AST/Hicksville, 1:1 by name),
  * maps ``cadence`` -> DeliveryCadence (A->Mon/Thu, B->Tue/Fri, Boxes->weekly
    box shipping),
  * writes the primary's MemberDietaryProfile (menu type + allergies; the CSV
    ``food_notes`` go to BOTH the member restrictions note AND the kitchen-facing
    food notes),
  * applies the global kitchen-output rules (``reconcile_member_kitchen_output``)
    so an unfulfillable combo is flagged Out of Orbit,
  * marks the enrollment Verified and activates service (Service Active),
    creating the delivery plan + calendar.

Re-runnable: existing enrollments are re-synced in place (kitchen, cadence,
address, dietary fields) rather than duplicated.

Usage:
    python manage.py import_kitchen_assignments                 # DRY RUN (rolls back)
    python manage.py import_kitchen_assignments --apply          # commit
    python manage.py import_kitchen_assignments --limit 50       # first 50 rows
    python manage.py import_kitchen_assignments --file path.csv
"""
import csv
import re
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
    ProductTypeKind,
)
from api.portal.serializers import internal_service_case
from api.serializers import sync_household_members
from api.services.delivery import (
    create_member_delivery_schedules,
    current_household_cadence,
    update_household_cadence,
)
from api.services.kitchens import kitchen_offered_menu_index
from api.services.lifecycle import advance_enrollment
from api.services.meal_rules import reconcile_member_kitchen_output
from api.services.orders import (
    generate_delivery_calendar,
    resync_scheduled_orders,
    sync_delivery_calendar,
)

_DEFAULT_FILE = "tmp/verification/Kitchen Assignment List with Cadence Facility.csv"

# Sheet Facility code -> Kitchen.name (1:1 for this list).
_FACILITY_TO_KITCHEN = {"eng": "ENG", "ast": "AST", "hicksville": "Hicksville"}
# Sheet Cadence code -> meal DeliveryCadence (Boxes handled separately).
_CADENCE_TO_DELIVERY = {"a": DeliveryCadence.MON_THU, "b": DeliveryCadence.TUE_FRI}

# Allergy label (lowercased) -> FoodAllergy code, built from the model choices.
_ALLERGY_BY_LABEL = {label.lower(): code for code, label in FoodAllergy.choices}
_ALLERGY_BY_LABEL["others"] = "other"  # sheet uses the plural

_BLANKS = {"", "#n/a", "n/a", "none"}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}
# Trailing "<STATE> <ZIP>" of an address string.
_TAIL_RE = re.compile(r",?\s*([A-Za-z]{2})\.?\s+(\d{5})(?:-\d{4})?\s*$")
# An apartment / unit designator embedded in the street line.
_UNIT_RE = re.compile(
    r"\b(?:APT|APARTMENT|UNIT|STE|SUITE|FL|FLOOR|RM|ROOM|BLDG|#)\s*\.?\s*([A-Za-z0-9\-]+)\b",
    re.I,
)


def _clean(value):
    v = (value or "").strip().strip('"').strip("\u201c\u201d").strip()
    return "" if v.lower() in _BLANKS else v


def _state_from_zip(zip_):
    """NY is authoritative from the ZIP (10001-14975) -- the sheet is full of
    "Ne" typos for NY that would otherwise parse as Nebraska."""
    try:
        n = int((zip_ or "")[:5])
    except ValueError:
        return ""
    return "NY" if 10001 <= n <= 14975 else ""


def _parse_address(raw):
    """Split "476 EAST NEW YORK AVENUE APT 1, BROOKLYN, NY 11225" into
    ``(street, unit, city, state, zip)``. Best-effort: any component may be
    blank when the source string is malformed."""
    a = _clean(raw)
    state = zip_ = ""
    m = _TAIL_RE.search(a)
    if m:
        zip_ = m.group(2)
        state = _state_from_zip(zip_) or (
            m.group(1).upper() if m.group(1).upper() in _US_STATES else m.group(1).upper()
        )
        a = a[: m.start()].strip().rstrip(",")
    parts = [p.strip() for p in a.split(",") if p.strip()]
    street = parts[0] if parts else ""
    city = parts[-1] if len(parts) > 1 else ""
    unit = ""
    um = _UNIT_RE.search(street)
    if um:
        unit = um.group(1)
        street = (street[: um.start()] + street[um.end():]).strip().rstrip(",").strip()
    return street, unit, city.title(), state, zip_


def _parse_allergies(raw):
    """Return ``(codes, unknown_labels)`` from a ';'-separated allergy string."""
    codes, unknown = [], []
    for tok in re.split(r"[;,]", raw or ""):
        tok = tok.strip().strip('"')
        low = tok.lower()
        if low in _BLANKS:
            continue
        code = _ALLERGY_BY_LABEL.get(low)
        if code and code != "none":
            codes.append(code)
        elif not code:
            unknown.append(tok)
    return list(dict.fromkeys(codes)), unknown


# Address types (priority order) usable as a delivery fallback when the sheet
# address is blank/unparseable.
_FALLBACK_ADDRESS_TYPES = (AddressType.CURRENT, AddressType.HOME, AddressType.MAILING)


class Command(BaseCommand):
    help = (
        "Import the flat Kitchen Assignment List (client_id, delivery_address, "
        "menu_type, food_notes, allergies, cadence, facility): configure each "
        "client's delivery address + dietary profile, assign the kitchen, apply "
        "the global kitchen-output rules and activate service. Dry-run unless "
        "--apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Path to the CSV.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N rows.")

    def handle(self, *args, **options):
        apply = options["apply"]
        path = options["file"]
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except FileNotFoundError:
            raise CommandError(f"CSV not found: {path!r}")
        if options["limit"]:
            rows = rows[: options["limit"]]

        self.kitchens = {k.name: k for k in Kitchen.objects.all()}
        # Case/whitespace-insensitive lookup so a "Hicksville" facility matches a
        # kitchen named "hicksville", "Hicksville Kitchen", etc.
        self.kitchens_by_norm = {
            k.name.strip().lower(): k for k in Kitchen.objects.all()
        }
        self._offered_cache = {}

        report = Counter()
        flags = []  # (client_id, reason)

        with transaction.atomic():
            for row in rows:
                cid = _clean(row.get("client_id"))
                try:
                    with transaction.atomic():
                        outcome = self._process_row(row, cid)
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                if outcome[0] not in ("activated", "activated_out_of_orbit"):
                    flags.append((cid, outcome[1] if len(outcome) > 1 else ""))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, flags, apply)

    # -- per-row ------------------------------------------------------------
    def _offered(self, kitchen):
        if kitchen.pk not in self._offered_cache:
            self._offered_cache[kitchen.pk] = kitchen_offered_menu_index(kitchen)
        return self._offered_cache[kitchen.pk]

    def _process_row(self, row, cid):
        if not cid:
            return ("skip_no_client_id",)
        client = Client.objects.filter(client_id=cid).first()
        if client is None:
            return ("skip_client_not_found", "client id not in DB")

        # Facility -> kitchen (required to activate). Match case/whitespace-
        # insensitively; also allow the facility value itself to be a kitchen
        # name not in the alias map.
        facility = _clean(row.get("facility")).lower()
        target = _FACILITY_TO_KITCHEN.get(facility, facility).strip().lower()
        kitchen = self.kitchens_by_norm.get(target)
        if kitchen is None:
            have = ", ".join(sorted(self.kitchens)) or "(none)"
            return ("skip_no_kitchen",
                    f"no kitchen for facility {facility!r} (looked for "
                    f"{target!r}); kitchens in DB: {have}")

        # Cadence -> DeliveryCadence + product kind.
        cadence_code = _clean(row.get("cadence")).lower()
        is_boxes = cadence_code == "boxes"
        if is_boxes:
            product_kind = ProductTypeKind.BOXES
            cadence = DeliveryCadence.ONCE_A_WEEK
        else:
            product_kind = ProductTypeKind.MEALS
            cadence = _CADENCE_TO_DELIVERY.get(cadence_code)
            if cadence is None:
                return ("skip_no_cadence", f"unmapped cadence {cadence_code!r}")

        menu_type = _clean(row.get("menu_type"))
        if not menu_type:
            return ("skip_no_menu", "blank menu type")

        # Household (client is the primary; reuse the one the internal-service
        # case auto-created).
        household = self._ensure_household(client)

        # Internal-service case drives the enrollment + activation window.
        case = internal_service_case(client)
        if case is None:
            return ("skip_no_internal_case", "no internal-service case")

        # Enrollment (reuse the client's existing one, else create).
        enr = (
            EnrollmentVerification.objects.filter(client=client)
            .order_by("-opened_at")
            .first()
        )
        created_enr = enr is None
        if created_enr:
            program = case.program if case.program_id else None
            enr = EnrollmentVerification.objects.create(
                client=client,
                household=household,
                case=case,
                program_name=(program.name if program else "") or case.program_name,
                service_type=case.service_type or "",
                household_size=household.members.count() or 1,
                stage=EnrollmentStage.PENDING_VERIFICATION,
            )

        # Delivery address (parsed from the sheet, fallback to an existing addr).
        address = self._delivery_address(client, enr, row.get("delivery_address"))
        if address is not None and enr.delivery_address_id != address.pk:
            enr.delivery_address = address
            enr.save(update_fields=["delivery_address"])

        # Make sure every roster member has a profile (dependents default to
        # Out of Orbit / blank until an agent configures them), then set the
        # PRIMARY's dietary fields from the sheet.
        sync_household_members(client, enr)
        food_codes, unknown = _parse_allergies(row.get("allergies"))
        food_notes = _clean(row.get("food_notes"))
        other_bits = []
        if food_notes:
            other_bits.append(food_notes)
        if unknown:
            other_bits.append(f"Allergies: {', '.join(unknown)}")

        profile = enr.member_profiles.filter(client=client).first()
        if profile is None:
            profile = MemberDietaryProfile.objects.create(
                enrollment=enr, client=client,
                member_name=f"{client.first_name or ''} {client.last_name or ''}".strip(),
            )
        profile.menu_type = menu_type
        profile.food_allergies = food_codes
        profile.other_dietary_restrictions = "; ".join(other_bits)
        profile.save(update_fields=[
            "menu_type", "food_allergies", "other_dietary_restrictions",
        ])

        # Assign the kitchen + apply the global kitchen-output rules to every
        # member (kitchen-aware: unfulfillable combos go Out of Orbit).
        enr.kitchen = kitchen
        enr.save(update_fields=["kitchen"])
        offered = self._offered(kitchen)
        primary_out = False
        for mv in enr.member_profiles.all():
            out, _became, _reason = reconcile_member_kitchen_output(
                mv, kitchen, offered=offered,
            )
            if mv.pk == profile.pk:
                primary_out = out
                # The sheet food_notes must ALSO reach the kitchen on the PO.
                if not out and food_notes:
                    note = (mv.kitchen_food_notes or "").strip()
                    mv.kitchen_food_notes = f"{note} | {food_notes}".strip(" |") \
                        if note else food_notes
                    mv.save(update_fields=["kitchen_food_notes"])

        # Verify -> activate. Only run the VERIFIED step when the enrollment is
        # still at Pending Verification (later stages -- Kitchen Assignment --
        # are already past Verified, so advancing back would be rejected).
        if enr.stage == EnrollmentStage.PENDING_VERIFICATION:
            advance_enrollment(
                enr, EnrollmentStage.VERIFIED, force=True,
                note="Imported from Kitchen Assignment List.",
            )

        schedules_existed = enr.delivery_schedules.exists()
        if not schedules_existed:
            create_member_delivery_schedules(
                enr, case=case, cadence=cadence, kitchen=kitchen,
                product_kind=product_kind,
            )
            generate_delivery_calendar(enr)
        else:
            # Re-sync: push the (possibly changed) kitchen + cadence onto the
            # existing plan + calendar.
            enr.delivery_schedules.update(kitchen=kitchen)
            if current_household_cadence(enr) != cadence:
                update_household_cadence(enr, cadence=cadence, case=case)
                sync_delivery_calendar(enr)
        enr.delivery_schedules.update(kitchen=kitchen)
        resync_scheduled_orders(enrollment=enr)

        if enr.stage != EnrollmentStage.SERVICE_ACTIVE:
            advance_enrollment(
                enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
                note=f"Imported; kitchen assigned ({kitchen.name}); service activated.",
            )

        return ("activated_out_of_orbit" if primary_out else "activated",)

    def _ensure_household(self, client):
        membership = (
            HouseholdMember.objects.filter(client=client)
            .select_related("household")
            .first()
        )
        if membership is not None:
            hh = membership.household
            if not membership.is_primary:
                # Shouldn't happen for this list (all are primaries); leave the
                # existing household as-is.
                return hh
            if not hh.name:
                hh.name = f"{(client.last_name or '').strip()} Household".strip()
                hh.save(update_fields=["name"])
            return hh
        hh = Household.objects.create(
            name=f"{(client.last_name or '').strip()} Household".strip()
        )
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        return hh

    def _delivery_address(self, client, enr, raw):
        street, unit, city, state, zip_ = _parse_address(raw)
        if street or zip_:
            addr = enr.delivery_address if (
                enr.delivery_address_id
                and enr.delivery_address.type == AddressType.DELIVERY
            ) else Address(client=client, type=AddressType.DELIVERY)
            addr.client = client
            addr.type = AddressType.DELIVERY
            addr.street = street[:255]
            addr.unit = unit[:60]
            addr.city = city[:120]
            addr.state = (state or "")[:2]
            addr.zip = zip_[:10]
            addr.save()
            return addr
        # Blank / unparseable sheet address -> reuse an existing delivery addr,
        # else copy the client's best residential address into a DELIVERY record.
        if enr.delivery_address_id:
            return enr.delivery_address
        src = self._fallback_address(client)
        if src is None:
            return None
        return Address.objects.create(
            client=client, type=AddressType.DELIVERY,
            street=src.street, unit=src.unit, city=src.city, county=src.county,
            state=src.state, zip=src.zip, notes=src.notes,
        )

    def _fallback_address(self, client):
        addrs = [
            a for a in client.addresses.all()
            if (a.street or a.city) and a.type != AddressType.DELIVERY
        ]
        if not addrs:
            return None
        for kind in _FALLBACK_ADDRESS_TYPES:
            for a in addrs:
                if a.type == kind:
                    return a
        return addrs[0]

    # -- report -------------------------------------------------------------
    def _report(self, report, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Kitchen Assignment List import ==="))
        order = [
            ("activated", "Service Active (verified + kitchen + activated)"),
            ("activated_out_of_orbit", "Activated, primary Out of Orbit (unfulfillable)"),
            ("skip_no_kitchen", "Skipped: unmapped facility"),
            ("skip_no_cadence", "Skipped: unmapped cadence"),
            ("skip_no_menu", "Skipped: blank menu type"),
            ("skip_no_internal_case", "Skipped: no internal-service case"),
            ("skip_client_not_found", "Skipped: client id not found"),
            ("skip_no_client_id", "Skipped: blank client id"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<48}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<48}: {sum(report.values())}")

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

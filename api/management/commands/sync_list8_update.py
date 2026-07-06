"""LIST 8 full-update pass ("Full Update + cadence facility + cleaned addresses").

This is the DATA pass for the LIST 8 roster (run BEFORE the lifecycle pass
``reconcile_member_stages``). Unlike the older ``sync_member_data`` (which read a
different, flat roster layout and touched only one profile per client), LIST 8
carries the full household: pre-split *AI-cleaned* delivery address, trustworthy
kitchen OUTPUTS for the primary, and per-member dietary INPUTS for every
household member.

Per row (keyed by the primary Unite Us client id in col A) and ONLY when a cell
is non-empty (a blank never wipes existing DB data):

  * delivery address  -> the enrollment's delivery Address (AI-cleaned cols
    D/E preferred, else raw B/C; city F, state G, zip H, notes I),
  * per-member dietary -> menu type / meal category / food allergies /
    restrictions / notes on EACH member's profile (primary from L-P, dependents
    from the HM #2..#9 blocks); a listed member without a profile gets one,
  * kitchen (facility) -> enrollment.kitchen (col BL),
  * cadence            -> enrollment.delivery_weekdays (col BK: A->Mon/Thu,
    B->Tue/Fri, Boxes->Wed),
  * kitchen output     -> the PRIMARY's kitchen meal type / food note are taken
    DIRECTLY from the sheet outputs (cols J/K, which are already calculated);
    dependents are run through the meal-rules engine (kitchen-aware), so an
    unfulfillable member is flagged Out of Orbit.

Already-active households whose kitchen or cadence changed have their LIVE plan
rebuilt (schedules re-pointed at the new kitchen, cadence re-applied, calendar
resynced, POs refreshed).

Enrollment STAGE moves (Pending Verification / Verified / Kitchen Assignment ->
Active/etc. by authorization + completeness) are intentionally NOT done here --
run ``reconcile_member_stages`` afterwards for that.

Usage:
    python manage.py sync_list8_update                       # DRY RUN (rolls back)
    python manage.py sync_list8_update --apply               # commit
    python manage.py sync_list8_update --limit 100           # first 100 rows
    python manage.py sync_list8_update --file path.xlsx
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import (
    Address,
    AddressType,
    Client,
    DeliveryCadence,
    EnrollmentStage,
    Household,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MemberStatus,
    ProductTypeKind,
)
from api.management.commands.import_meal_verifications import (
    _clean,
    _client_id,
    _parse_allergies,
    _parse_meal_category,
    _parse_restrictions,
    _profile_fields,
    _read_rows,
)
from api.management.commands.import_list2_review import _STATE_FIX
from api.services.delivery import (
    current_household_cadence,
    update_household_cadence,
)
from api.services.kitchens import kitchen_offered_menu_index
from api.services.lifecycle import governing_internal_case
from api.services.meal_rules import apply_to_member, reconcile_member_kitchen_output
from api.services.orders import resync_scheduled_orders, sync_delivery_calendar
from api.services.sheet_import import CADENCE_TO_WEEKDAYS, resolve_kitchen

_DEFAULT_FILE = "tmp/verification/LIST8-Full Update + cadence facility + cleaned addresses.xlsx"

# --- column letters (tab "07.02.26", read by letter) -----------------------
_C_PRIMARY = "A"
_C_STREET_RAW, _C_APT_RAW = "B", "C"
_C_STREET, _C_APT = "D", "E"          # AI-cleaned (preferred)
_C_CITY, _C_STATE, _C_ZIP, _C_NOTES = "F", "G", "H", "I"
_C_MEAL_OUTPUT, _C_FOOD_NOTE_OUTPUT = "J", "K"
_C_MEAL_CAT, _C_ALLERGY, _C_OTHER_ALLERGY, _C_OTHER_RESTR, _C_GEN_NOTE = "L", "M", "N", "O", "P"
_C_TOTAL = "Q"
# HM #2..#9 dependent blocks: (client_id, meal_cat, food_allergies, other_allergies, other_restr).
_DEP_BLOCKS = [
    ("R", "S", "T", "U", "V"),
    ("W", "X", "Y", "Z", "AA"),
    ("AB", "AC", "AD", "AE", "AF"),
    ("AG", "AH", "AI", "AJ", "AK"),
    ("AL", "AM", "AN", "AO", "AP"),
    ("AQ", "AR", "AS", "AT", "AU"),
    ("AV", "AW", "AX", "AY", "AZ"),
    ("BA", "BB", "BC", "BD", "BE"),
]
_C_CADENCE, _C_FACILITY = "BK", "BL"

# Sheet cadence code -> (DeliveryCadence, ProductTypeKind) for a plan rebuild.
_CADENCE_PLAN = {
    "a": (DeliveryCadence.MON_THU, ProductTypeKind.MEALS),
    "b": (DeliveryCadence.TUE_FRI, ProductTypeKind.MEALS),
    "boxes": (DeliveryCadence.ONCE_A_WEEK, ProductTypeKind.BOXES),
}


def _primary_fields(cells):
    """Primary member's dietary from cols L-P. Returns ``(fields, kind)``."""
    menu, category, kind = _parse_meal_category(cells.get(_C_MEAL_CAT))
    food, unknown_food = _parse_allergies(cells.get(_C_ALLERGY))
    restrictions, other_restr = _parse_restrictions(cells.get(_C_OTHER_RESTR))
    other_bits = []
    other_allergens = _clean(cells.get(_C_OTHER_ALLERGY))
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
        "general_verification_notes": _clean(cells.get(_C_GEN_NOTE)),
    }, kind


class Command(BaseCommand):
    help = (
        "LIST 8 full-update data pass: refresh delivery address, per-member "
        "dietary, kitchen (facility), cadence and the primary's kitchen output "
        "(from the sheet), rebuilding live plans when an active household's "
        "kitchen/cadence changed. No stage moves (run reconcile_member_stages "
        "after). Dry-run unless --apply."
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

        self.kitchens_by_norm = {
            k.name.strip().lower(): k for k in Kitchen.objects.all()
        }
        self._offered_cache = {}

        report = Counter()
        fields = Counter()
        flags = []

        with transaction.atomic():
            for row in rows:
                pid = _client_id(row.get(_C_PRIMARY))
                try:
                    with transaction.atomic():
                        outcome = self._process_row(row, pid, fields)
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                # Only surface ACTIONABLE outcomes (errors, unmapped facility).
                # The bulk "no enrollment" / "not in DB" skips are expected noise
                # (they belong to other lists) and are counted, not listed.
                if outcome[0] not in (
                    "updated", "no_change", "skip_no_enrollment", "skip_not_in_db",
                ):
                    flags.append((f"{outcome[0]} {pid}", outcome[1] if len(outcome) > 1 else ""))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, fields, flags, apply)

    # -- helpers ------------------------------------------------------------
    def _offered(self, kitchen):
        if kitchen.pk not in self._offered_cache:
            self._offered_cache[kitchen.pk] = kitchen_offered_menu_index(kitchen)
        return self._offered_cache[kitchen.pk]

    def _process_row(self, cells, pid, fields):
        primary = Client.objects.filter(client_id=pid).first()
        if primary is None:
            return ("skip_not_in_db", "primary id not in DB")
        enr = (
            primary.enrollments.select_related("kitchen", "delivery_address", "client")
            .order_by("-opened_at")
            .first()
        )
        if enr is None:
            return ("skip_no_enrollment", "primary has no enrollment")

        changed = set()

        # --- delivery address ---
        if self._update_address(primary, enr, cells):
            changed.add("delivery_address")

        # --- kitchen (facility) ---
        old_kitchen_id = enr.kitchen_id
        facility = _clean(cells.get(_C_FACILITY))
        if facility:
            kitchen, target = resolve_kitchen(self.kitchens_by_norm, facility)
            if kitchen is None:
                return ("skip_bad_facility",
                        f"unmapped facility {facility!r} (looked for {target!r})")
            if enr.kitchen_id != kitchen.pk:
                enr.kitchen = kitchen
                enr.save(update_fields=["kitchen"])
                changed.add("kitchen")
        kitchen = enr.kitchen
        kitchen_changed = "kitchen" in changed

        # --- per-member dietary inputs ---
        profiles = {
            p.client_id: p
            for p in enr.member_profiles.select_related("client").all()
        }
        # Primary.
        prim_prof = self._member_profile(enr, primary, profiles)
        pf, _pkind = _primary_fields(cells)
        if self._apply_profile_inputs(prim_prof, pf):
            changed.add("primary_dietary")
        # Dependents (HM #2..#9).
        for block in _DEP_BLOCKS:
            mid = _client_id(cells.get(block[0]))
            if not mid or mid == pid:
                continue
            dep = Client.objects.filter(client_id=mid).first()
            if dep is None:
                continue
            dep_prof = self._member_profile(enr, dep, profiles)
            dfields, _dkind = _profile_fields(block, cells)
            if self._apply_profile_inputs(dep_prof, dfields):
                changed.add("dependent_dietary")

        # --- kitchen output (primary trusts the sheet; dependents via engine) ---
        offered = self._offered(kitchen) if kitchen is not None else None
        if self._update_outputs(enr, primary, cells, kitchen, offered):
            changed.add("kitchen_output")

        # --- cadence + live-plan rebuild ---
        cad_code = _clean(cells.get(_C_CADENCE)).lower()
        weekdays = CADENCE_TO_WEEKDAYS.get(cad_code)
        has_plan = enr.delivery_schedules.exists()
        cadence_changed = weekdays is not None and set(enr.delivery_weekdays or []) != set(weekdays)

        if weekdays is not None and cadence_changed and not has_plan:
            # No live plan yet: safe to just record the weekdays.
            enr.delivery_weekdays = weekdays
            enr.save(update_fields=["delivery_weekdays"])
            changed.add("cadence")

        # Already-active (or otherwise planned) household whose kitchen or
        # cadence changed -> rebuild the live plan so deliveries/POs follow.
        if has_plan and (kitchen_changed or cadence_changed) and kitchen is not None:
            self._rebuild_plan(enr, kitchen, cad_code)
            changed.add("plan_rebuilt")

        for c in changed:
            fields[c] += 1
        return ("updated",) if changed else ("no_change",)

    def _member_profile(self, enr, client, profiles):
        """Return (creating if needed) the member's dietary profile + ensure a
        household membership so the roster is complete for the lifecycle pass.

        A ``Client`` can belong to at most ONE household (unique constraint), so
        we only attach a membership when the client isn't already in one; a
        client already placed in another household keeps that placement (we still
        record their dietary profile under this enrollment)."""
        prof = profiles.get(client.client_id)
        if prof is not None:
            return prof
        if enr.household_id and not HouseholdMember.objects.filter(client=client).exists():
            HouseholdMember.objects.create(
                household_id=enr.household_id, client=client,
                is_primary=client.client_id == enr.client_id,
            )
        prof = MemberDietaryProfile.objects.create(
            enrollment=enr,
            client=client,
            member_name=f"{client.first_name or ''} {client.last_name or ''}".strip(),
        )
        profiles[client.client_id] = prof
        return prof

    def _apply_profile_inputs(self, profile, f):
        """Overwrite dietary INPUT fields from the sheet, but only where the
        sheet value is non-empty (blank never wipes). Returns True on change."""
        changed = []
        for name in ("menu_type", "meal_category", "general_verification_notes",
                     "other_dietary_restrictions"):
            v = f.get(name)
            if v and getattr(profile, name) != v:
                setattr(profile, name, v)
                changed.append(name)
        for name in ("food_allergies", "dietary_restrictions"):
            v = f.get(name)
            if v and list(getattr(profile, name) or []) != list(v):
                setattr(profile, name, v)
                changed.append(name)
        if changed:
            profile.save(update_fields=changed + ["updated_at"])
        return bool(changed)

    def _update_outputs(self, enr, primary, cells, kitchen, offered):
        """Set the kitchen output for every member. The PRIMARY takes the sheet's
        (already-calculated) J/K outputs; dependents run through the meal-rules
        engine (kitchen-aware when a kitchen is assigned). Returns True on change."""
        touched = False
        meal_out = _clean(cells.get(_C_MEAL_OUTPUT))
        food_note = _clean(cells.get(_C_FOOD_NOTE_OUTPUT))
        for prof in enr.member_profiles.select_related("client").all():
            is_primary = prof.client_id == primary.client_id
            if is_primary and meal_out:
                new = dict(
                    status=MemberStatus.ACTIVE,
                    kitchen_meal_type=meal_out,
                    kitchen_food_notes=food_note,
                )
                if any(getattr(prof, k) != v for k, v in new.items()):
                    for k, v in new.items():
                        setattr(prof, k, v)
                    prof.save(update_fields=list(new) + ["updated_at"])
                    touched = True
                continue
            if not (prof.menu_type or "").strip():
                continue  # nothing to derive from yet
            before = (prof.status, prof.kitchen_meal_type, prof.kitchen_food_notes)
            if kitchen is not None:
                reconcile_member_kitchen_output(prof, kitchen, offered=offered, save=True)
            else:
                apply_to_member(prof)
            if before != (prof.status, prof.kitchen_meal_type, prof.kitchen_food_notes):
                touched = True
        return touched

    def _rebuild_plan(self, enr, kitchen, cad_code):
        """Re-point an already-planned household's live schedules at ``kitchen``
        and re-apply the cadence, then resync the calendar + scheduled POs."""
        case = governing_internal_case(enr)
        cadence, _kind = _CADENCE_PLAN.get(cad_code, (None, None))
        enr.delivery_schedules.update(kitchen=kitchen)
        if cadence is not None and current_household_cadence(enr) != cadence:
            update_household_cadence(enr, cadence=cadence, case=case)
            sync_delivery_calendar(enr)
        enr.delivery_schedules.update(kitchen=kitchen)
        resync_scheduled_orders(enrollment=enr)

    def _update_address(self, primary, enr, cells):
        """Update the enrollment's delivery Address in place from the sheet's
        split (AI-cleaned) columns. Returns True on change."""
        street = _clean(cells.get(_C_STREET)) or _clean(cells.get(_C_STREET_RAW))
        unit = _clean(cells.get(_C_APT)) or _clean(cells.get(_C_APT_RAW))
        city = _clean(cells.get(_C_CITY))
        raw_state = _clean(cells.get(_C_STATE))
        state = _STATE_FIX.get(raw_state.lower(), raw_state[:2].upper())
        zip_ = _clean(cells.get(_C_ZIP))
        notes = _clean(cells.get(_C_NOTES))
        if not (street or zip_):
            return False
        new = dict(
            street=street[:255], unit=unit[:60], city=city[:120],
            state=state[:2], zip=zip_[:10], notes=notes,
        )
        addr = enr.delivery_address if (
            enr.delivery_address_id
            and enr.delivery_address.type == AddressType.DELIVERY
        ) else None
        if addr is not None:
            if all(getattr(addr, k) == v for k, v in new.items()):
                return False
            for k, v in new.items():
                setattr(addr, k, v)
            addr.save(update_fields=list(new))
            return True
        addr = Address.objects.create(client=primary, type=AddressType.DELIVERY, **new)
        enr.delivery_address = addr
        enr.save(update_fields=["delivery_address"])
        return True

    # -- report -------------------------------------------------------------
    def _report(self, report, fields, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== LIST 8 full-update (data pass) ==="))
        order = [
            ("updated", "Households updated"),
            ("no_change", "Already up to date (no change)"),
            ("skip_no_enrollment", "Skipped: primary has no enrollment"),
            ("skip_not_in_db", "Skipped: primary id not in DB"),
            ("skip_bad_facility", "Skipped: unmapped facility"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<46}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<46}: {sum(report.values())}")

        if fields:
            self.stdout.write(head("\nField updates (households touched):"))
            for name in ("delivery_address", "kitchen", "cadence", "primary_dietary",
                         "dependent_dietary", "kitchen_output", "plan_rebuilt"):
                if fields.get(name):
                    self.stdout.write(f"  {name:<46}: {fields[name]}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 30):"))
            for pid, reason in flags[:30]:
                self.stdout.write(f"  {pid or '(blank)'}: {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

"""Import the "LIST 2 - Meal Inputs Not Trustworthy, No Meal Outputs" sheet.

These members have dietary inputs but no trustworthy meal output, so they are
NEVER activated. Instead we build the household + enrollment + per-member
dietary profiles (capturing food restrictions, menu/meal type, etc.) and then
set a per-row state + open a review ticket, driven by the sheet's column A flag.

Column A ("for wb") -> action:
    Out of Orbit - Food                     -> Out of Orbit       (no ticket)
    6/25 - Not Yet Part of Analysis         -> Out of Orbit       (+ ticket)
    Out of Orbit - "Other" Issue            -> Out of Orbit       (+ ticket)
    No Meal Info                            -> Needs Verification (+ ticket)
    Out of Range Zip Code Client            -> Out of Orbit       (+ ticket)
    Final Verification Shows "Ineligible"   -> Ineligible         (+ ticket)
    Showing Needs Verification              -> Out of Orbit       (+ ticket)
    Out of Orbit - One-Off Notes Issue      -> Out of Orbit       (+ ticket)

State realization (enrollment is never activated):
    Out of Orbit       -> enrollment forced to Verified; every member dietary
                          profile status set to OUT_OF_ORBIT.
    Needs Verification -> enrollment left at Pending Verification.
    Ineligible         -> enrollment forced to Denied (lifecycle Not Eligible).

The review ticket is a High-severity "Status Check" (source Other) linked to
the member + case, reason "Client need Review - <column A value>".

Column mapping (differs from the Trustworthy sheet -- shifted by the leading
flag column): A=flag, B=primary id, C-F=address, G=address notes, T=total
members, BN=Cadence, BO=Facility (both ~blank here). PRIMARY dietary comes from
the AI-RE columns O=menu, P=allergies, Q=restrictions, R=other-allergens,
S=notes; DEPENDENTS (HM #2..#10) use their raw 5-col blocks.

Usage:
    python manage.py import_list2_review --file "tmp/verification/LIST 2 ....xlsx"
    python manage.py import_list2_review --file "..." --apply
"""
import re
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
    MemberStatus,
    Ticket,
    TicketSeverity,
    TicketSource,
    TicketStatus,
    TicketType,
    TicketTypeCode,
)
from api.management.commands.import_meal_verifications import (
    _clean,
    _parse_allergies,
    _parse_meal_category,
    _parse_restrictions,
    _profile_fields,
    _read_rows,
)
from api.portal.serializers import internal_service_case
from api.services.lifecycle import advance_enrollment, recompute_client_stage

_COL_FLAG, _COL_PRIMARY, _COL_TOTAL = "A", "B", "T"
_COL_STREET, _COL_CITY, _COL_STATE, _COL_ZIP, _COL_ADDR_NOTES = "C", "D", "E", "F", "G"
# Primary dietary lives in the AI-RE columns: menu, allergies, restrictions,
# other-allergens, general notes.
_AI_MENU, _AI_ALLERGY, _AI_RESTR, _AI_OTHER, _AI_NOTES = "O", "P", "Q", "R", "S"
# Dependent HM #2..#10 raw 5-col blocks: (id, meal_cat, allergies, other, restr).
_DEP_BLOCKS = [
    ("U", "V", "W", "X", "Y"),
    ("Z", "AA", "AB", "AC", "AD"),
    ("AE", "AF", "AG", "AH", "AI"),
    ("AJ", "AK", "AL", "AM", "AN"),
    ("AO", "AP", "AQ", "AR", "AS"),
    ("AT", "AU", "AV", "AW", "AX"),
    ("AY", "AZ", "BA", "BB", "BC"),
    ("BD", "BE", "BF", "BG", "BH"),
    ("BI", "BJ", "BK", "BL", "BM"),
]

_OUT_OF_ORBIT, _NEEDS_VERIFICATION, _INELIGIBLE = (
    "out_of_orbit",
    "needs_verification",
    "ineligible",
)
# Normalized column-A flag -> (state, open_ticket?).
_SCENARIO = {
    "out of orbit - food": (_OUT_OF_ORBIT, False),
    "6/25 - not yet part of analysis": (_OUT_OF_ORBIT, True),
    'out of orbit - "other" issue': (_OUT_OF_ORBIT, True),
    "no meal info": (_NEEDS_VERIFICATION, True),
    "out of range zip code client": (_OUT_OF_ORBIT, True),
    'final verification shows "ineligible"': (_INELIGIBLE, True),
    "showing needs verification": (_OUT_OF_ORBIT, True),
    "out of orbit - one-off notes issue": (_OUT_OF_ORBIT, True),
}

_OPEN_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
_RESTR_BLANKS = {"", "no restrictions", "none", "no restriction"}
_STATE_FIX = {"new york": "NY", "ny": "NY"}


def _norm_flag(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _ai_allergies(raw):
    """Parse the AI-RE allergy list; the AI writes 'Others' for the catch-all
    Other allergy, which the base parser leaves as unknown."""
    codes, unknown = _parse_allergies(raw)
    if re.search(r"\bothers?\b", (raw or "").lower()) and "other" not in codes:
        codes.append("other")
    unknown = [u for u in unknown if u.lower() not in ("other", "others")]
    return codes, unknown


def _primary_profile_fields(cells):
    """Dietary fields for the PRIMARY from the AI-RE columns (O-S)."""
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
        "Import the LIST 2 'Not Trustworthy / No Meal Outputs' sheet: build "
        "households + dietary profiles, set per-row state (Out of Orbit / Needs "
        "Verification / Ineligible) from column A, and open Status Check tickets. "
        "Dry-run unless --apply."
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

        self.status_check_type, _ = TicketType.objects.get_or_create(
            code=TicketTypeCode.STATUS_CHECK,
            defaults={"label": TicketTypeCode.STATUS_CHECK.label},
        )

        # Clients listed as a dependent in any row -> skip their own primary row.
        listed_as_member = set()
        for cells in rows:
            for block in _DEP_BLOCKS:
                mid = _clean(cells.get(block[0]))
                if mid:
                    listed_as_member.add(mid)

        report = Counter()
        tickets = Counter()
        flags = []  # (primary_id, note)

        with transaction.atomic():
            for cells in rows:
                primary_id = _clean(cells.get(_COL_PRIMARY))
                try:
                    with transaction.atomic():
                        key, made_ticket, note = self._process_row(
                            cells, primary_id, listed_as_member
                        )
                except Exception as exc:  # isolate a bad row, keep going
                    key, made_ticket, note = ("error", False, str(exc))
                report[key] += 1
                if made_ticket:
                    tickets["created"] += 1
                if key.startswith("skip") or key == "error":
                    flags.append((primary_id, note))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, tickets, flags, apply)

    def _process_row(self, cells, primary_id, listed_as_member):
        flag = _norm_flag(cells.get(_COL_FLAG))
        scenario = _SCENARIO.get(flag)
        if scenario is None:
            return ("skip_unknown_flag", False, f"unmapped column A: {flag!r}")
        state, wants_ticket = scenario

        if not primary_id:
            return ("skip_no_primary_id", False, "blank primary id")
        primary = Client.objects.filter(client_id=primary_id).first()
        if primary is None:
            return ("skip_primary_not_found", False, "primary id not in DB")
        if primary.enrollments.exists():
            return ("skip_already_enrolled", False, "primary already enrolled")
        if primary_id in listed_as_member:
            return ("skip_member_of_other_household", False, "listed as a dependent elsewhere")

        case = internal_service_case(primary)
        if case is None:
            return ("skip_no_internal_case", False, "primary has no internal-service case")

        household, member_clients, block_for = self._build_household(
            primary, primary_id, cells
        )
        if household is None:
            return ("skip_member_of_other_household", False, "primary is a dependent in another household")

        try:
            total = int(float(cells.get(_COL_TOTAL) or 1))
        except (TypeError, ValueError):
            total = 1

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
                block = block_for.get(str(m.client_id))
                fields, _kind = _profile_fields(block, cells)
            MemberDietaryProfile.objects.create(
                enrollment=enr,
                client=m,
                member_name=f"{m.first_name or ''} {m.last_name or ''}".strip(),
                **fields,
            )

        self._apply_state(enr, state, member_clients)

        if wants_ticket:
            made = self._open_status_check(primary, case, cells.get(_COL_FLAG))
        else:
            made = False
        return (state, made, "")

    def _apply_state(self, enr, state, member_clients):
        if state == _OUT_OF_ORBIT:
            advance_enrollment(
                enr, EnrollmentStage.VERIFIED, force=True,
                note="LIST 2 import: verified, members set Out of Orbit.",
            )
            now = timezone.now()
            for profile in enr.member_profiles.all():
                profile.status = MemberStatus.OUT_OF_ORBIT
                profile.updated_at = now
                profile.save(update_fields=["status", "updated_at"])
        elif state == _INELIGIBLE:
            # Ineligible final verification: there is no longer a DENIED
            # enrollment stage (authorization/eligibility outcomes are separate
            # from the verification stage). Leave the enrollment at Pending
            # Verification and recompute the members' funnel stages.
            for m in member_clients:
                recompute_client_stage(m)
        else:  # needs verification -> leave at Pending Verification
            for m in member_clients:
                recompute_client_stage(m)

    def _build_household(self, primary, primary_id, cells):
        """Reuse the primary's auto-created solo household (from the case import)
        or create one, then fold in any dependent HM rows. Returns
        (household, member_clients, block_for) or (None, ...) when the primary
        actually belongs to another family."""
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

    def _open_status_check(self, primary, case, flag_value):
        flag = (flag_value or "").strip()
        reason = (
            "Status check needed: the LIST 2 verification review flagged this "
            + (f"member as '{flag}'. " if flag else "member for review. ")
            + "Review the member's eligibility and meal details, and confirm "
            "whether service should be (re)started, kept Out of Orbit, or closed."
        )
        existing = Ticket.objects.filter(
            type=self.status_check_type, status__in=_OPEN_STATUSES,
            client=primary, case=case,
        ).first()
        if existing:
            return False
        Ticket.objects.create(
            type=self.status_check_type,
            severity=TicketSeverity.HIGH,
            source=TicketSource.OTHER,
            reason=reason,
            client=primary,
            case=case,
        )
        return True

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

    def _report(self, report, tickets, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== LIST 2 review import ==="))
        order = [
            (_OUT_OF_ORBIT, "Out of Orbit"),
            (_NEEDS_VERIFICATION, "Needs Verification"),
            (_INELIGIBLE, "Ineligible"),
            ("skip_already_enrolled", "Skipped: already enrolled"),
            ("skip_no_internal_case", "Skipped: no internal-service case"),
            ("skip_member_of_other_household", "Skipped: dependent in another household"),
            ("skip_primary_not_found", "Skipped: primary id not found"),
            ("skip_no_primary_id", "Skipped: blank primary id"),
            ("skip_unknown_flag", "Skipped: unmapped column A flag"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<42}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<42}: {sum(report.values())}")
        self.stdout.write(f"  {'Status Check tickets created':<42}: {tickets.get('created', 0)}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 30):"))
            for pid, note in flags[:30]:
                self.stdout.write(f"  {pid or '(blank)'}: {note}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

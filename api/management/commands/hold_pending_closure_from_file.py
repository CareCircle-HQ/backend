"""One-off: hold a roster of members from a flat client sheet.

Handles both the AAA "Pending Closure" roster (AAA-PendingClosure.xlsx) and the
BBB "To Be On Hold" roster (BBB-ToBeOnHold.xlsx). They share the same columns;
the only difference is whether a Case Closure ticket is created:

  * AAA: run WITH tickets (default).
  * BBB: run with ``--no-tickets`` (hold + info only).

For every client in the sheet this:

  1. **Creates a Case Closure ticket** (``TicketTypeCode.CASE_CLOSURE``) whose
     ``reason`` comes from the sheet's "REASON for Not on PO" column (e.g.
     "Pending Closure", "Verification or IS Case Denied", "Services Paused").
     De-duplicated on (client, type, reason): a member that already has an
     unresolved ticket of the same type + reason is skipped, so re-runs never
     pile up duplicate tickets for the same member + reason. Skipped entirely
     when ``--no-tickets`` is passed.

  2. **Puts the member On Hold** -- and because a hold lives on the household's
     enrollment (it drops the whole household off the Purchase Order), the hold
     is applied HOUSEHOLD-WIDE, anchored on the household primary:
       * if the household already has an enrollment, it is moved to ON_HOLD
         (unless already ON_HOLD);
       * otherwise a minimal enrollment is created and moved to ON_HOLD.
     ``advance_enrollment`` cascades the lifecycle to every household member, so
     a primary with dependents holds the entire household. A household is held
     only once even if several of its members appear in the sheet.

  3. **Updates the dietary + address info** from the sheet onto the held
     enrollment's MemberDietaryProfile (menu type, food allergies, dietary
     restrictions, notes) and sets the household delivery address + notes.

Dry-run unless --apply; --force is required to COMMIT when warnings exist
(file members that are dependents whose primary isn't in the sheet -> holding
their household also holds the non-listed primary).

The hold ``reason`` (sheet column G) is recorded on the On-Hold StageEvent note.

Usage:
    # AAA Pending Closure (with tickets)
    python manage.py hold_pending_closure_from_file --apply --force
    # BBB To Be On Hold (no tickets)
    python manage.py hold_pending_closure_from_file \
        --file tmp/verification/BBB-ToBeOnHold.xlsx --no-tickets --apply --force
    # DDD Cases Closed -- mark CANCELLED (members Inactive), no tickets
    python manage.py hold_pending_closure_from_file \
        --file tmp/verification/DDD-CasesClosed-MealInfo.xlsx \
        --no-tickets --cancel --apply --force
    # MRN Manual Review -- hold + info + Status Check ticket (desc from column G)
    python manage.py hold_pending_closure_from_file \
        --file tmp/verification/MRN-ManualReviewNeeded.xlsx \
        --ticket-type status_check --apply --force
"""
from collections import Counter, defaultdict

import openpyxl
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.management.commands.activate_members_from_file import (
    _MENU_ALIAS,
    _MENU_TO_CATEGORY,
    parse_allergies,
    parse_restrictions,
)
from api.models import (
    Address,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    MemberDietaryProfile,
    MemberStatus,
    MenuCategory,
    MenuType,
    Ticket,
    TicketStatus,
    TicketType,
    TicketTypeCode,
)
from api.serializers import ensure_household_with_primary
from api.services.catalog import menu_type_for_member
from api.services.lifecycle import advance_enrollment

_DEFAULT_FILE = "tmp/verification/AAA-PendingClosure.xlsx"

_COL = {
    "id": "Unite Us Client ID",
    "street": "Address - Street (AI Cleaned)",
    "apt": "Address-Apt (AI Cleaned)",
    "city": "Address - City",
    "state": "Address - State",
    "zip": "Address - Postal Code",
    "reason": "REASON for Not on PO",
    "addr_notes": "Address Notes",
    "meal": "Meal Category (Input)",
    "allergy": "Allergy Note (Input)",
    "other_allergy": "Other Allergy Note",
    "other_restr": "Other Restrictions",
    "verif_note": "General Verification Note",
}


def _norm(v):
    return "" if v is None else str(v).strip()


def _get_client(cid):
    """Look up a client by id, tolerating a malformed (non-UUID) id in the
    sheet -- returns None instead of raising so the row is reported as missing."""
    try:
        return Client.objects.filter(client_id=cid).first()
    except (ValueError, ValidationError):
        return None


def _read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    header = [_norm(c) for c in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    col = {k: idx.get(name) for k, name in _COL.items()}
    # The reason column is headed "REASON for Not on PO" on some rosters and
    # just "REASON" on others -- accept either.
    if col.get("reason") is None:
        col["reason"] = idx.get("REASON")
    out = []
    for r in rows[1:]:
        rec = {k: (_norm(r[i]) if i is not None and i < len(r) else "") for k, i in col.items()}
        if rec["id"]:
            rec["id"] = rec["id"].lower()
            out.append(rec)
    return out


class Command(BaseCommand):
    help = (
        "Create Case Closure tickets, put members (household-wide) On Hold, and "
        "update dietary/address info from the AAA Pending Closure roster. "
        "Dry-run unless --apply; --force to commit past warnings."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE)
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--force", action="store_true",
            help="Required with --apply to COMMIT when warnings exist.",
        )
        parser.add_argument(
            "--no-tickets", action="store_true",
            help="Skip Case Closure ticket creation (BBB To Be On Hold roster).",
        )
        parser.add_argument(
            "--cancel", action="store_true",
            help="Mark the household CANCELLED (members Inactive) instead of "
                 "On Hold (DDD Cases Closed roster).",
        )
        parser.add_argument(
            "--skip-meal-info", action="store_true",
            help="Don't write dietary profiles from the sheet (EEE Cases Closed "
                 "with no meal info): only update address + set the stage.",
        )
        parser.add_argument(
            "--ticket-type", default=TicketTypeCode.CASE_CLOSURE,
            choices=[c for c, _ in TicketTypeCode.choices],
            help="Ticket type code to open (default case_closure; e.g. "
                 "status_check for the MRN Manual Review roster).",
        )

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]
        force = options["force"]
        self.create_tickets = not options["no_tickets"]
        self.cancel = options["cancel"]
        self.skip_meal_info = options["skip_meal_info"]
        self.target_stage = (
            EnrollmentStage.CANCELLED if self.cancel else EnrollmentStage.ON_HOLD
        )
        self.action_verb = "cancelled" if self.cancel else "placed on hold"

        rows = _read_rows(path)
        if not rows:
            self.stdout.write(self.style.ERROR(f"No rows read from {path}."))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{'Cancel' if self.cancel else 'Hold'} roster: {path} -> "
            f"{len(rows)} client rows"
            + ("" if self.create_tickets else " [--no-tickets]")
        ))

        self.menus = {m.name.strip().lower(): m.name for m in MenuType.objects.all()}
        self.cc_type = None
        if self.create_tickets:
            code = options["ticket_type"]
            self.cc_type = TicketType.objects.filter(code=code).first()
            if self.cc_type is None:
                self.stdout.write(self.style.ERROR(
                    f"No '{code}' TicketType found; cannot create tickets."
                ))
                return

        report = Counter()
        self.missing = []
        self.dependents = []          # file member is a dependent (warning)
        self.multi_file_households = []
        self.blocked = False

        with transaction.atomic():
            # Resolve clients + group file rows by household.
            resolved = {}   # client_id -> (client, rec)
            for rec in rows:
                client = _get_client(rec["id"])
                if client is None:
                    self.missing.append(rec["id"])
                    report["missing"] += 1
                    continue
                resolved[rec["id"]] = (client, rec)

            # ---- Tickets (optional). They don't need an enrollment, so create
            # them up front, deduped on (client, type, reason). ----
            if self.create_tickets:
                for cid, (client, rec) in resolved.items():
                    if self._ensure_ticket(client, rec["reason"]):
                        report["ticket_created"] += 1
                    else:
                        report["ticket_skipped"] += 1

            # ---- Group by household so each household is held exactly once. ----
            households = defaultdict(list)  # household -> [(client, rec), ...]
            for cid, (client, rec) in resolved.items():
                household = ensure_household_with_primary(client)
                households[household.pk].append((household, client, rec))

            for hpk, entries in households.items():
                household = entries[0][0]
                if len(entries) > 1:
                    self.multi_file_households.append(
                        (str(hpk), [str(c.client_id) for _, c, _ in entries])
                    )
                bucket = self._hold_household(household, entries)
                report[bucket] += 1
                report["info_updated"] += len(entries)

            # Warnings: dependents whose hold pulls in a non-listed primary.
            has_warnings = bool(self.dependents)
            if not apply:
                transaction.set_rollback(True)
            elif has_warnings and not force:
                transaction.set_rollback(True)
                self.blocked = True

        self._report(report, apply, force, len(rows))

    # -- tickets -----------------------------------------------------------
    def _ensure_ticket(self, client, reason):
        """Create a Case Closure ticket unless an unresolved one with the same
        (client, type, reason) already exists. Returns True if created."""
        exists = (
            Ticket.objects.filter(client=client, type=self.cc_type, reason=reason)
            .exclude(status=TicketStatus.RESOLVED)
            .exists()
        )
        if exists:
            return False
        Ticket.objects.create(
            type=self.cc_type,
            status=TicketStatus.OPEN,
            reason=reason,
            client=client,
        )
        return True

    # -- hold --------------------------------------------------------------
    def _hold_household(self, household, entries):
        """Put the whole household On Hold (reusing an existing enrollment or
        creating one), and write the file's dietary/address info onto the member
        profiles. Returns a report bucket."""
        file_by_client = {c.client_id: rec for _, c, rec in entries}

        # A file member that is NOT this household's primary means holding the
        # household also holds the (non-listed) primary -> warn.
        primary_hm = household.members.filter(is_primary=True).select_related("client").first()
        for _, client, _rec in entries:
            hm = household.members.filter(client=client).first()
            if hm is not None and not hm.is_primary:
                self.dependents.append(str(client.client_id))

        # Existing household enrollment (most recent) or a fresh one.
        enr = household.enrollment_verifications.order_by("-opened_at").first()
        created_enrollment = False
        if enr is None:
            primary_client = primary_hm.client if primary_hm else entries[0][1]
            enr = EnrollmentVerification.objects.create(
                client=primary_client,
                household=household,
                stage=EnrollmentStage.PENDING_VERIFICATION,
            )
            created_enrollment = True

        # Delivery address from the sheet (prefer the primary's row, else any
        # file member's row). Attach to the household enrollment.
        addr_rec = None
        if primary_hm and primary_hm.client_id in file_by_client:
            addr_rec = file_by_client[primary_hm.client_id]
        elif entries:
            addr_rec = entries[0][2]
        if addr_rec and any(addr_rec[k] for k in ("street", "city", "state", "zip")):
            address = Address.objects.create(
                client=enr.client, type="temporary",
                street=addr_rec["street"], unit=addr_rec["apt"], city=addr_rec["city"],
                state=addr_rec["state"], zip=addr_rec["zip"], notes=addr_rec["addr_notes"],
            )
            enr.delivery_address = address
            enr.save(update_fields=["delivery_address"])

        # Ensure a MemberDietaryProfile per household member; apply the file's
        # dietary data to members that appear in the sheet. Skipped entirely when
        # the roster carries no meal info (--skip-meal-info).
        for hm in ([] if self.skip_meal_info else household.members.select_related("client").all()):
            if not hm.client_id:
                continue
            rec = file_by_client.get(hm.client_id)
            profile = MemberDietaryProfile.objects.filter(
                enrollment=enr, client=hm.client
            ).first()
            if rec is not None:
                menu = self._resolve_menu(rec["meal"]) or menu_type_for_member(
                    food_allergies=[], meal_category=rec["meal"]
                )
                allergies, unknown_al = parse_allergies(rec["allergy"])
                restrictions, unknown_re = parse_restrictions(rec["other_restr"])
                notes = " | ".join(
                    x for x in ([rec["other_allergy"]] + unknown_al + unknown_re) if x
                )
                fields = dict(
                    member_name=f"{hm.client.first_name} {hm.client.last_name}".strip(),
                    menu_type=menu,
                    food_allergies=allergies,
                    dietary_restrictions=restrictions,
                    other_dietary_restrictions=notes,
                    meal_category=_MENU_TO_CATEGORY.get(menu.lower(), MenuCategory.FRESH_MEAL),
                    general_verification_notes=rec["verif_note"],
                )
                if profile is None:
                    MemberDietaryProfile.objects.create(
                        enrollment=enr, client=hm.client, status=MemberStatus.ACTIVE,
                        **fields,
                    )
                else:
                    for k, v in fields.items():
                        setattr(profile, k, v)
                    profile.save()
            elif profile is None:
                # Non-file household member: minimal profile so the household is
                # represented on the enrollment.
                MemberDietaryProfile.objects.create(
                    enrollment=enr, client=hm.client,
                    member_name=f"{hm.client.first_name} {hm.client.last_name}".strip(),
                    menu_type=menu_type_for_member(),
                    status=MemberStatus.ACTIVE,
                )

        # Advance to the target stage (ON_HOLD or CANCELLED), cascading to the
        # whole household. Skip if already there. When cancelling, mark every
        # member profile Inactive so the member dimension agrees with the
        # cancelled enrollment (both exclude them from all POs/deliveries).
        if enr.stage == self.target_stage:
            return "already_at_target"
        if self.cancel:
            enr.member_profiles.update(status=MemberStatus.INACTIVE)
        reason = (addr_rec or {}).get("reason", "") if addr_rec else ""
        note = f"Roster import: {self.action_verb}."
        if reason:
            note += f" Reason: {reason}"
        advance_enrollment(enr, self.target_stage, force=True, note=note)
        return "target_new_enrollment" if created_enrollment else "target_existing_enrollment"

    def _resolve_menu(self, meal_category):
        key = meal_category.strip().lower()
        key = _MENU_ALIAS.get(key, key)
        return self.menus.get(key)

    # -- report ------------------------------------------------------------
    def _report(self, report, apply, force, total):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Summary ==="))
        self.stdout.write(self.style.SUCCESS(
            f"  Tickets created                    : {report.get('ticket_created', 0)}"
        ))
        self.stdout.write(
            f"  Tickets skipped (already exist)    : {report.get('ticket_skipped', 0)}"
        )
        verb = "Cancelled" if self.cancel else "Held"
        self.stdout.write(self.style.SUCCESS(
            f"  Households {verb} (new enrollment)   : {report.get('target_new_enrollment', 0)}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Households {verb} (existing enroll.) : {report.get('target_existing_enrollment', 0)}"
        ))
        self.stdout.write(
            f"  Households already at target stage : {report.get('already_at_target', 0)}"
        )
        self.stdout.write(
            f"  Member info updated                : {report.get('info_updated', 0)}"
        )
        self.stdout.write(self.style.WARNING(
            f"  Missing from DB                    : {report.get('missing', 0)}"
        ))
        self.stdout.write(f"  {'TOTAL rows':<34}: {total}")

        if self.multi_file_households:
            self.stdout.write(head(
                f"\nHouseholds with >1 file member ({len(self.multi_file_households)}): "
                "held once, each member ticketed:"
            ))
            for hpk, members in self.multi_file_households[:30]:
                self.stdout.write(f"  household {hpk}: {', '.join(members)}")

        if self.missing:
            self.stdout.write(head(f"\nMissing from DB ({len(self.missing)}, up to 60):"))
            for cid in self.missing[:60]:
                self.stdout.write(f"  {cid}")

        if self.dependents:
            self.stdout.write(self.style.ERROR(
                f"\n!!! WARNING: {len(self.dependents)} file member(s) are household "
                "DEPENDENTS -- holding their household also holds the non-listed "
                "primary. Review before committing. !!!"
            ))
            for cid in self.dependents[:40]:
                self.stdout.write(f"  {cid}")

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

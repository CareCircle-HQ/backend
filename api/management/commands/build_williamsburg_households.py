"""One-off: build + activate the Williamsburg households from the roster sheets.

This is the BACKFILL counterpart to the go-forward Williamsburg exception
(``api.services.williamsburg`` / ``Client.is_williamsburg``). For each roster
row it:

  * marks the primary + every listed member as Williamsburg
    (``is_williamsburg=True`` and ``lead_source="Williamsburg"`` so a future
    re-sync keeps the flag),
  * builds the household (primary + members), reusing an existing household and
    never restructuring one a client already belongs to,
  * sets ``total_family_members`` / ``is_a_family`` on the primary,
  * creates a verification enrollment and runs the SAME fast-track used by the
    extension hook, which assigns the Williamsburg kitchen, gives every member
    the Kosher dietary profile (Pork + Shellfish as kitchen notes, kept Active),
    defaults the delivery address to the primary's current address, builds the
    Mon/Thu delivery schedule + calendar, and advances to SERVICE_ACTIVE.

Idempotent: a primary that already has an enrollment is skipped (never
clobbered). Rosters are read by HEADER NAME (the two sheets have different
column layouts).

Usage:
    python manage.py build_williamsburg_households            # dry run
    python manage.py build_williamsburg_households --apply     # commit
    python manage.py build_williamsburg_households --file a.xlsx --file b.xlsx
"""
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    Household,
    HouseholdMember,
)
from api.portal.serializers import internal_service_case
from api.services.williamsburg import fast_track_williamsburg_enrollment

_DEFAULT_FILES = [
    "tmp/verification/WilliamsburgClients1.xlsx",
    "tmp/verification/WilliamsburgClients2.xlsx",
]
_PRIMARY_COL = "Unite Us Client ID"
_HM_COLS = [f"HM #{n} - Enrollment Platform Client ID" for n in range(2, 11)]


def _norm(value):
    return "" if value is None else str(value).strip()


def _read_households(path):
    """Yield (primary_id, [member_ids]) per roster row, keyed by header name."""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    header = [_norm(c) for c in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    pi = idx.get(_PRIMARY_COL)
    hmi = [idx[c] for c in _HM_COLS if c in idx]
    out = []
    for r in rows[1:]:
        primary = _norm(r[pi]).lower() if pi is not None and pi < len(r) else ""
        if not primary:
            continue
        members = [
            _norm(r[i]).lower() for i in hmi if i < len(r) and _norm(r[i])
        ]
        out.append((primary, members))
    return out


class Command(BaseCommand):
    help = (
        "Build + activate the Williamsburg households from the roster sheets, "
        "applying the Williamsburg rules (Kosher, Williamsburg kitchen, Service "
        "Active). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", action="append", dest="files",
            help="Roster .xlsx (repeatable). Defaults to the two Williamsburg sheets.",
        )
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        files = options.get("files") or _DEFAULT_FILES
        apply = options["apply"]

        report = Counter()
        members_stat = Counter()
        flags = []

        households = []
        for path in files:
            households.extend(_read_households(path))

        with transaction.atomic():
            for primary_id, member_ids in households:
                try:
                    with transaction.atomic():
                        key, note = self._process(primary_id, member_ids, members_stat)
                except Exception as exc:  # isolate a bad row
                    key, note = ("error", str(exc))
                report[key] += 1
                if key != "activated":
                    flags.append((primary_id, note))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, members_stat, flags, apply, len(households))

    def _process(self, primary_id, member_ids, members_stat):
        primary = Client.objects.filter(client_id=primary_id).first()
        if primary is None:
            return ("skip_primary_not_found", "primary id not in DB")
        if primary.enrollments.exists():
            return ("skip_already_enrolled", "primary already has an enrollment")

        household, why = self._reuse_or_create_household(primary)
        if household is None:
            return ("skip_dependent_primary", why)

        # Attach the listed members to the primary's household.
        for mid in member_ids:
            member = Client.objects.filter(client_id=mid).first()
            if member is None:
                members_stat["missing"] += 1
                continue
            existing = HouseholdMember.objects.filter(client=member).first()
            if existing is not None:
                if existing.household_id == household.household_id:
                    members_stat["already_in_household"] += 1
                else:
                    members_stat["in_other_household"] += 1
                continue
            HouseholdMember.objects.create(
                household=household, client=member, is_primary=False
            )
            members_stat["attached"] += 1

        # Mark the whole household as Williamsburg (operational flag + canonical
        # lead_source so a future re-sync keeps the flag).
        member_clients = [
            hm.client for hm in household.members.select_related("client").all()
            if hm.client_id
        ]
        size = len(member_clients)
        for c in member_clients:
            updates = []
            if not c.is_williamsburg:
                c.is_williamsburg = True
                updates.append("is_williamsburg")
            if (c.lead_source or "").strip().lower() != "williamsburg":
                c.lead_source = "Williamsburg"
                updates.append("lead_source")
            if updates:
                c.save(update_fields=updates)

        if primary.total_family_members != size or primary.is_a_family != (size > 1):
            primary.total_family_members = size
            primary.is_a_family = size > 1
            primary.save(update_fields=["total_family_members", "is_a_family"])

        # Create the enrollment, then run the SAME fast-track as the ext hook.
        case = internal_service_case(primary)
        program = case.program if (case and case.program_id) else None
        enr = EnrollmentVerification.objects.create(
            client=primary,
            household=household,
            case=case,
            program_name=(program.name if program else "") or (case.program_name if case else ""),
            service_type=(case.service_type if case else "") or "",
            household_size=size,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        fast_track_williamsburg_enrollment(enr, actor=None, agent=None)
        return ("activated", "")

    def _reuse_or_create_household(self, primary):
        membership = (
            HouseholdMember.objects.filter(client=primary)
            .select_related("household")
            .first()
        )
        if membership is not None:
            if not membership.is_primary:
                return None, "primary is a dependent in another household"
            return membership.household, ""
        household = Household.objects.create(
            name=f"{(primary.last_name or '').strip()} Household".strip()
        )
        HouseholdMember.objects.create(
            household=household, client=primary, is_primary=True
        )
        return household, ""

    def _report(self, report, members_stat, flags, apply, total):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Build Williamsburg households ==="))
        order = [
            ("activated", "Activated (Service Active)"),
            ("skip_already_enrolled", "Skipped: primary already enrolled"),
            ("skip_dependent_primary", "Skipped: primary is a dependent elsewhere"),
            ("skip_primary_not_found", "Skipped: primary id not found"),
            ("error", "Errored (row rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<46}: {report[key]}")
        self.stdout.write(f"  {'TOTAL households':<46}: {total}")

        self.stdout.write(head("\nMembers:"))
        self.stdout.write(f"  {'attached (new)':<46}: {members_stat.get('attached', 0)}")
        self.stdout.write(f"  {'already in this household':<46}: {members_stat.get('already_in_household', 0)}")
        self.stdout.write(f"  {'in another household (left alone)':<46}: {members_stat.get('in_other_household', 0)}")
        self.stdout.write(f"  {'listed but not found in DB':<46}: {members_stat.get('missing', 0)}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, up to 40):"))
            for pid, note in flags[:40]:
                self.stdout.write(f"  {pid}: {note}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

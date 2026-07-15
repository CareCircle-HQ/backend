"""Reactivate long-standing Out-of-Range members and (re)apply kitchen rules.

Targets every member currently **Out of Range** whose household's GOVERNING
internal-service case was opened before a cutoff date (default 2026-04-01), and:

  * **Force-reactivates** the member (``OUT_OF_RANGE`` -> ``ACTIVE``), regardless
    of whether the delivery/primary ZIP is still outside the coverage area. No
    system notes or timeline events are written (per request). The household's
    On-Hold state is intentionally LEFT UNTOUCHED -- being Out of Range is not,
    on its own, a reason to lift a hold.

  * For every target household WITH a kitchen assigned: re-applies the kitchen's
    cadence rules (keeping a still-valid existing cadence, otherwise the
    kitchen's first active cadence) and rebuilds the delivery calendar.

  * For every target household WITHOUT a kitchen: leaves it unconfigured so it is
    surfaced on the Care Management page (via the warnings snapshot). NOTE: an
    On-Hold household is excluded from the Care Management queue until the hold
    is lifted -- see ``_can_access`` / ``SERVICE_EXCLUDED_ENROLLMENT_STAGES``.

The warnings snapshot is refreshed per household so any other actionable
condition (multiple open cases, internal case expired, insurance expiring, ...)
is current.

Idempotent-ish and safe: a member already ACTIVE is skipped. Dry-run unless
``--apply``.

Usage:
    python manage.py reactivate_out_of_range_members                 # DRY RUN
    python manage.py reactivate_out_of_range_members --apply          # commit
    python manage.py reactivate_out_of_range_members --limit 10       # first 10 households
    python manage.py reactivate_out_of_range_members --cutoff 2026-04-01
"""
from collections import Counter, defaultdict
from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import (
    DeliveryCadence,
    MemberDietaryProfile,
    MemberStatus,
)
from api.services.catalog import product_kind_for_enrollment
from api.services.delivery import current_household_cadence, update_household_cadence
from api.services.lifecycle import governing_internal_case
from api.services.meal_rules import resolve_kitchen_meal
from api.services.orders import sync_delivery_calendar
from api.services.warnings import CARE_MANAGEMENT_CODES, evaluate_enrollment_warnings
from api.services.warnings import sync_household_warnings

DEFAULT_CUTOFF = date(2026, 4, 1)


class Command(BaseCommand):
    help = (
        "Force-reactivate Out-of-Range members whose governing internal-service "
        "case opened before the cutoff; apply kitchen cadence rules + rebuild the "
        "calendar when a kitchen is assigned, else leave for Care Management. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--limit", type=int, default=0, help="Process first N households."
        )
        parser.add_argument(
            "--cutoff",
            type=str,
            default=DEFAULT_CUTOFF.isoformat(),
            help="Only households whose governing internal case opened BEFORE this "
            "date (YYYY-MM-DD). Default 2026-04-01.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        try:
            cutoff = datetime.strptime(options["cutoff"], "%Y-%m-%d").date()
        except ValueError:
            raise CommandError("--cutoff must be YYYY-MM-DD")

        head = self.style.MIGRATE_HEADING
        self.stdout.write(head(f"\nCutoff (opened before): {cutoff.isoformat()}"))

        # All Out-of-Range members, grouped by household enrollment.
        profiles = list(
            MemberDietaryProfile.objects.filter(status=MemberStatus.OUT_OF_RANGE)
            .select_related(
                "client", "enrollment", "enrollment__kitchen", "enrollment__client"
            )
            .prefetch_related(
                "enrollment__client__cases",
                "enrollment__delivery_schedules",
                "enrollment__kitchen__cadences",
            )
        )
        by_enr = defaultdict(list)
        enr_map = {}
        for p in profiles:
            if not p.enrollment_id:
                continue
            by_enr[p.enrollment_id].append(p)
            enr_map[p.enrollment_id] = p.enrollment

        # Target households: governing internal case opened before the cutoff.
        target_ids = []
        for eid, enr in enr_map.items():
            case = governing_internal_case(enr)
            if case and case.date_opened and case.date_opened.date() < cutoff:
                target_ids.append(eid)
        if options["limit"]:
            target_ids = target_ids[: options["limit"]]

        target_members = sum(len(by_enr[eid]) for eid in target_ids)
        self.stdout.write(
            f"  Out-of-Range members            : {len(profiles)} "
            f"across {len(enr_map)} households"
        )
        self.stdout.write(
            f"  TARGET (opened before cutoff)   : {target_members} members "
            f"across {len(target_ids)} households\n"
        )

        report = Counter()
        cal = Counter()
        cm_tally = Counter()

        with transaction.atomic():
            for eid in target_ids:
                enr = enr_map[eid]
                try:
                    with transaction.atomic():
                        self._process_household(enr, by_enr[eid], report, cal, cm_tally)
                except Exception as exc:  # isolate a bad household, keep going
                    report["errors"] += 1
                    self.stdout.write(self.style.ERROR(f"  enr {eid}: {exc}"))

            self._report(report, cal, cm_tally, apply)

            if not apply:
                transaction.set_rollback(True)

    def _process_household(self, enrollment, oor_members, report, cal, cm_tally):
        report["households_processed"] += 1

        # 1) Force-reactivate each Out-of-Range member. No notes / timeline.
        #    Restore the kitchen-agnostic meal type/notes (cleared when the member
        #    went Out of Range) so the rebuilt calendar carries real values.
        for mp in oor_members:
            if mp.status != MemberStatus.OUT_OF_RANGE:
                continue
            result = resolve_kitchen_meal(mp.menu_type, mp.food_allergies)
            mp.status = MemberStatus.ACTIVE
            mp.kitchen_meal_type = result.kitchen_meal_type
            mp.kitchen_food_notes = result.kitchen_food_notes
            mp.save(
                update_fields=[
                    "status",
                    "kitchen_meal_type",
                    "kitchen_food_notes",
                    "updated_at",
                ]
            )
            report["members_reactivated"] += 1

        # 2) Per household: apply kitchen rules + rebuild, or leave for Care Mgmt.
        if enrollment.kitchen_id:
            cadence = self._choose_cadence(enrollment)
            if not cadence:
                # Kitchen assigned but the kitchen runs no active cadence and the
                # household has none either -> can't apply rules; flag for review.
                report["kitchen_but_no_cadence_available"] += 1
            else:
                once_weekday = None
                if cadence == DeliveryCadence.ONCE_A_WEEK:
                    wd = list(enrollment.delivery_weekdays or [])
                    once_weekday = wd[0] if wd else None
                case = governing_internal_case(enrollment) or enrollment.case
                kind = product_kind_for_enrollment(enrollment)
                update_household_cadence(
                    enrollment,
                    cadence=cadence,
                    once_a_week_weekday=once_weekday,
                    case=case,
                    product_kind=kind,
                )
                res = sync_delivery_calendar(enrollment)
                cal["added"] += res.get("added", 0)
                cal["removed"] += res.get("removed", 0)
                cal["updated"] += res.get("updated", 0)
                report["kitchen_rules_applied"] += 1
        else:
            report["no_kitchen_left_for_care_management"] += 1

        # 3) Refresh the warnings snapshot; tally other actionable conditions.
        try:
            detected = sync_household_warnings(enrollment)
        except Exception:
            detected = evaluate_enrollment_warnings(enrollment)
        for w in detected:
            if w.code in CARE_MANAGEMENT_CODES:
                cm_tally[w.code] += 1

    @staticmethod
    def _choose_cadence(enrollment):
        """Keep a still-valid existing cadence, else the kitchen's first active
        cadence. Returns "" when nothing is available."""
        existing = current_household_cadence(enrollment)
        kitchen = enrollment.kitchen
        active_codes = (
            [c.code for c in kitchen.cadences.all() if c.is_active] if kitchen else []
        )
        if existing and (not active_codes or existing in active_codes):
            return existing
        if active_codes:
            return active_codes[0]
        return existing or ""

    def _report(self, report, cal, cm_tally, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Reactivate Out-of-Range members ==="))
        rows = [
            ("Households processed", report.get("households_processed", 0)),
            ("Members force-reactivated", report.get("members_reactivated", 0)),
            ("Kitchen rules applied + calendar rebuilt", report.get("kitchen_rules_applied", 0)),
            ("Kitchen but no cadence available", report.get("kitchen_but_no_cadence_available", 0)),
            ("No kitchen -> left for Care Management", report.get("no_kitchen_left_for_care_management", 0)),
            ("Errored", report.get("errors", 0)),
        ]
        for label, value in rows:
            self.stdout.write(f"  {label:<42}: {value}")

        self.stdout.write(
            f"  {'Calendar occurrences (add/upd/del)':<42}: "
            f"{cal.get('added', 0)}/{cal.get('updated', 0)}/{cal.get('removed', 0)}"
        )

        if cm_tally:
            self.stdout.write(head("\n  Other actionable Care-Management conditions on targets:"))
            for code, n in sorted(cm_tally.items(), key=lambda kv: (-kv[1], kv[0])):
                self.stdout.write(f"    {code:<34}: {n}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN: rolled back. Re-run with --apply to commit."
                )
            )

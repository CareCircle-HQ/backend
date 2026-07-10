"""Reconcile households whose stored delivery cadence is NOT one their assigned
kitchen actually runs, and rebuild their delivery calendar to match.

Before delivery cadences became kitchen-scoped/data-driven, re-assigning a
household to a new kitchen could leave the OLD kitchen's cadence on the plan
(e.g. a household under Rockland -- which runs only ``tue_only`` -- still stuck
on ``once_a_week``). That mismatch means the plan's delivery weekday(s) don't
match what the kitchen fulfills, so Purchase Orders get cut for the wrong day.

For every non-terminal household with a kitchen + a delivery plan whose stored
cadence isn't in its kitchen's configured cadences, this command:

  * **auto-reassigns** the cadence when the kitchen runs exactly ONE active
    cadence -- re-applying it via ``update_household_cadence`` (recomputes
    delivery weekdays, first delivery, per-delivery quantity + totals on every
    plan) and then rebuilding the dated delivery calendar via
    ``sync_delivery_calendar`` so FUTURE occurrences move onto the new cadence's
    day(s) (dates already batched into a Purchase Order are left intact);
  * **flags** households whose kitchen runs multiple (or zero) active cadences --
    the intended one can't be guessed, so an agent must pick it.

Idempotent (a household already matching its kitchen is skipped). Dry-run unless
``--apply``.

Usage:
    python manage.py reassign_mismatched_cadence              # DRY RUN
    python manage.py reassign_mismatched_cadence --apply       # commit
    python manage.py reassign_mismatched_cadence --limit 10
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.catalog import product_kind_for_enrollment
from api.services.delivery import (
    active_cadence_codes,
    cadence_needs_weekday,
    current_household_cadence,
    update_household_cadence,
)
from api.services.lifecycle import governing_internal_case
from api.services.orders import _WEEKDAY_CODES, sync_delivery_calendar
from api.models import ProductTypeKind

_TERMINAL_STAGES = (
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
)


class Command(BaseCommand):
    help = (
        "Reassign the delivery cadence for households whose stored cadence isn't "
        "one their assigned kitchen runs, and rebuild the delivery calendar. "
        "Auto-fixes single-cadence kitchens; flags multi-cadence ones. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N households.")

    def handle(self, *args, **options):
        apply = options["apply"]
        active_codes = set(active_cadence_codes())

        candidates = (
            EnrollmentVerification.objects
            .exclude(stage__in=_TERMINAL_STAGES)
            .filter(kitchen__isnull=False)
            .select_related("kitchen", "client")
            .prefetch_related("kitchen__cadences")
        )

        report = Counter()
        flagged = []           # (client_id, kitchen, stored_cadence, kitchen_codes)
        fixed_rows = []        # (client_id, kitchen, old, new, added, removed)
        added_total = removed_total = 0
        processed = 0

        with transaction.atomic():
            for enr in candidates.iterator(chunk_size=500):
                if not enr.delivery_schedules.exists():
                    continue
                stored = current_household_cadence(enr)
                if not stored:
                    continue
                kitchen_codes = {c.code for c in enr.kitchen.cadences.all()}
                if stored in kitchen_codes:
                    continue  # already consistent

                report["mismatched"] += 1
                if options["limit"] and processed >= options["limit"]:
                    continue

                # Cadences the kitchen runs that are still ACTIVE globally.
                usable = sorted(kitchen_codes & active_codes)
                if len(usable) != 1:
                    report["flagged"] += 1
                    flagged.append((
                        str(enr.client_id), enr.kitchen.name, stored, sorted(kitchen_codes),
                    ))
                    continue

                target = usable[0]
                processed += 1
                try:
                    with transaction.atomic():
                        res = self._reassign(enr, target)
                    report["fixed"] += 1
                    added_total += res["added"]
                    removed_total += res["removed"]
                    fixed_rows.append((
                        str(enr.client_id), enr.kitchen.name, stored, target,
                        res["added"], res["removed"],
                    ))
                except Exception as exc:  # isolate a bad household; keep going
                    report["errors"] += 1
                    self.stdout.write(self.style.ERROR(f"  {enr.client_id}: {exc}"))

            self._report(report, fixed_rows, flagged, added_total, removed_total, apply)

            if not apply:
                transaction.set_rollback(True)

    def _reassign(self, enr, target):
        """Re-apply ``target`` cadence to the household's plan and rebuild the
        delivery calendar. Returns the sync result ``{added, removed, updated}``."""
        case = governing_internal_case(enr) or enr.case
        kind = product_kind_for_enrollment(enr)

        # A once-a-week style target needs a single delivery weekday: keep the
        # household's existing one when valid, else fall back to the kind default
        # (boxes -> Wednesday, meals -> Monday). Fixed-weekday cadences ignore it.
        once_weekday = None
        if cadence_needs_weekday(target):
            existing = [w for w in (enr.delivery_weekdays or []) if w in _WEEKDAY_CODES]
            once_weekday = existing[0] if existing else (
                "wed" if kind == ProductTypeKind.BOXES else "mon"
            )

        update_household_cadence(
            enr, cadence=target, once_a_week_weekday=once_weekday,
            case=case, product_kind=kind,
        )
        # Rebuild the dated calendar so future occurrences move onto the new
        # cadence's day(s); PO-committed dates are preserved by the sync.
        return sync_delivery_calendar(enr)

    def _report(self, report, fixed_rows, flagged, added, removed, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Reassign mismatched cadence ==="))
        self.stdout.write(f"  {'Households mismatched':<34}: {report.get('mismatched', 0)}")
        self.stdout.write(f"  {'Auto-reassigned (single cadence)':<34}: {report.get('fixed', 0)}")
        self.stdout.write(f"  {'Flagged (multi/zero cadence)':<34}: {report.get('flagged', 0)}")
        self.stdout.write(f"  {'Calendar occurrences added':<34}: {added}")
        self.stdout.write(f"  {'Calendar occurrences removed':<34}: {removed}")
        self.stdout.write(f"  {'Errored':<34}: {report.get('errors', 0)}")

        if fixed_rows:
            self.stdout.write(head("\nReassigned (up to 30):"))
            for cid, kitchen, old, new, a, r in fixed_rows[:30]:
                self.stdout.write(f"  {cid}  {kitchen}: {old} -> {new}  (+{a}/-{r} dates)")

        if flagged:
            self.stdout.write(head(f"\nFlagged -- kitchen runs multiple/zero cadences ({len(flagged)}):"))
            for cid, kitchen, stored, codes in flagged[:30]:
                self.stdout.write(f"  {cid}  {kitchen}: stored '{stored}', kitchen runs {codes}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

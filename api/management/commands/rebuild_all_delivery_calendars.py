"""One-time backfill: create missing delivery plans + rebuild the calendar for
every active household.

Fixes the backlog of members who were added to an ALREADY-active household after
its first kitchen assignment. Those members never got a
:class:`~api.models.MemberDeliverySchedule` (plans are created once, at kitchen
assignment), so they were absent from the delivery calendar and every future
Purchase Order.

For each in-service enrollment that already has a delivery plan (i.e. a chosen
cadence to snapshot from), this runs
:func:`~api.services.orders.rebuild_delivery_calendar`, which:
  * creates a plan for each active member missing one
    (:func:`~api.services.delivery.ensure_member_delivery_schedules`), then
  * reconciles the dated calendar -- adding the new members' future occurrences,
    NEVER touching a date already batched into a Purchase Order.

Out-of-orbit / paused / out-of-range members are skipped (they must not be
force-scheduled). Idempotent: re-running once everything is in sync is a no-op.

``--dry-run`` reports what WOULD change (households + member plans that would be
created) without writing anything -- run this first to review the blast radius.

Usage:
    python manage.py rebuild_all_delivery_calendars --dry-run
    python manage.py rebuild_all_delivery_calendars
    python manage.py rebuild_all_delivery_calendars --from 2026-07-20
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from api.models import (
    EnrollmentVerification,
    ScheduleStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from api.services.delivery import current_household_cadence
from api.services.orders import rebuild_delivery_calendar


class Command(BaseCommand):
    help = (
        "Backfill delivery plans + calendar for every active household so members "
        "added after kitchen assignment appear on future Purchase Orders."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--from",
            dest="from_date",
            default=None,
            help=(
                "Only reconcile occurrences on or after this ISO date "
                "(YYYY-MM-DD). Defaults to today."
            ),
        )

    def handle(self, *args, **options):
        from_date = None
        raw = options.get("from_date")
        if raw:
            try:
                from_date = date.fromisoformat(raw)
            except ValueError:
                raise CommandError(f"Invalid --from date: {raw!r} (use YYYY-MM-DD).")

        dry_run = options.get("dry_run", False)

        # In-service households that already have a plan (a cadence to snapshot).
        enrollments = (
            EnrollmentVerification.objects.exclude(
                stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES
            )
            .filter(delivery_schedules__status=ScheduleStatus.SCHEDULED)
            .distinct()
        )

        mode = "DRY RUN — no changes will be written" if dry_run else "APPLYING changes"
        self.stdout.write(f"Backfilling delivery calendars ({mode})...")

        scanned = 0
        households_with_missing = 0
        totals = {"plans_created": 0, "added": 0, "removed": 0, "updated": 0}

        for enr in enrollments.iterator(chunk_size=500):
            scanned += 1
            missing = self._missing_members(enr)
            if not missing:
                continue

            households_with_missing += 1
            label = self._label(enr)
            if dry_run:
                totals["plans_created"] += len(missing)
                self.stdout.write(
                    f"  would create {len(missing)} plan(s): {label} "
                    f"— {', '.join(missing)}"
                )
                continue

            res = rebuild_delivery_calendar(enr, from_date=from_date)
            totals["plans_created"] += res.get("plans_created", 0)
            totals["added"] += res.get("added", 0)
            totals["removed"] += res.get("removed", 0)
            totals["updated"] += res.get("updated", 0)
            self.stdout.write(
                f"  rebuilt: {label} — {res.get('plans_created', 0)} plan(s), "
                f"{res.get('added', 0)} occurrence(s) added"
            )

        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN: {scanned} household(s) scanned · "
                f"{households_with_missing} with missing members · "
                f"{totals['plans_created']} member plan(s) WOULD be created. "
                f"Re-run without --dry-run to apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done: {scanned} household(s) scanned · "
                f"{households_with_missing} fixed · "
                f"{totals['plans_created']} member plan(s) created · "
                f"{totals['added']} occurrence(s) added · "
                f"{totals['removed']} removed · {totals['updated']} updated."
            ))

    def _missing_members(self, enr):
        """Names of active member profiles on ``enr`` that have no delivery plan.

        Mirrors ensure_member_delivery_schedules' eligibility: a household cadence
        must exist (else there's nothing to snapshot from), and out-of-orbit /
        paused / excluded members are skipped."""
        if not current_household_cadence(enr):
            return []
        planned = set(
            enr.delivery_schedules.values_list("member_profile_id", flat=True)
        )
        missing = (
            enr.member_profiles.exclude(status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
            .exclude(pk__in=planned)
        )
        return [m.member_name or str(m.client_id) for m in missing]

    def _label(self, enr):
        client = getattr(enr, "client", None)
        who = (
            f"{client.first_name} {client.last_name}".strip() if client else ""
        ) or "Unknown"
        return f"enrollment {enr.pk} ({who})"

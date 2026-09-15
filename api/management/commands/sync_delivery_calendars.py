"""Reconcile the delivery calendar for every active household.

Ensures no eligible member is missing from upcoming Purchase Orders. For each
active enrollment it:
  * CREATES a delivery plan for any active member missing one (a member added to
    an already-active household never got a plan at kitchen-assignment time, so
    they were absent from the calendar + every future PO), then
  * re-syncs the dated :class:`~api.models.OrderSchedule` calendar with the
    current member plans + dietary profiles -- adding occurrences for
    members/dates that are missing, removing occurrences no longer planned, and
    refreshing the kitchen / menu / allergy snapshots.
Dates already batched into a PO are never touched.

This is the batch/ops counterpart to the per-edit resync that the portal runs
automatically (kitchen/menu/cadence edits) and the PO popup "Refresh" button.
Safe to run repeatedly; idempotent once everything is in sync.

Usage:
    python manage.py sync_delivery_calendars
    python manage.py sync_delivery_calendars --from 2026-07-06
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from api.services.orders import sync_active_calendars


class Command(BaseCommand):
    help = (
        "Reconcile the delivery calendar for active households so no eligible "
        "member is missing from upcoming Purchase Orders."
    )

    def add_arguments(self, parser):
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

        self.stdout.write("Reconciling active delivery calendars...")

        # sync_active_calendars already accepts a progress callback (the
        # "Prepare Members for PO" task uses it to drive a percentage in the UI);
        # this command simply never passed one. So an operator watching the log
        # saw a single line and then silence for minutes, with no way to tell a
        # long run from a hung one -- which is exactly the question being asked
        # when someone tails the log.
        state = {"last": -1}

        def progress(processed, total):
            if not total:
                return
            pct = processed * 100 // total
            # Every 5% -- enough to show life, not enough to spam a log file.
            if pct >= state["last"] + 5:
                state["last"] = pct
                self.stdout.write(f"  {pct:3d}%  {processed}/{total} enrollments")
                self.stdout.flush()

        totals = sync_active_calendars(from_date=from_date, progress_cb=progress)
        self.stdout.write(self.style.SUCCESS(
            f"Done: {totals['enrollments']} enrollments · "
            f"{totals.get('plans_created', 0)} member plans created · "
            f"{totals['added']} occurrences added · "
            f"{totals['removed']} removed · {totals['updated']} updated."
        ))

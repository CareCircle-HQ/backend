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
        totals = sync_active_calendars(from_date=from_date)
        self.stdout.write(self.style.SUCCESS(
            f"Done: {totals['enrollments']} enrollments · "
            f"{totals.get('plans_created', 0)} member plans created · "
            f"{totals['added']} occurrences added · "
            f"{totals['removed']} removed · {totals['updated']} updated."
        ))

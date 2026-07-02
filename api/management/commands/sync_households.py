"""Reconcile disconnected household members across all households.

A household member can be created two ways that historically did NOT converge:
the extension's household picker (a ``HouseholdMember`` roster row only) and the
verification wizard (a ``HouseholdMember`` row AND a ``MemberDietaryProfile`` on
the enrollment). Only the dietary profile surfaces on the CRM Household tab, so
picker-added members were invisible/disconnected.

This command runs the same two-way reconcile the API now does on every
household add / Household-tab load, but for ALL existing households at once --
fixing members already tied before the fix shipped. Idempotent.

    python manage.py sync_households
    python manage.py sync_households --dry-run           # report only, no writes
    python manage.py sync_households --client-id <uuid>   # one household; repeatable
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Household
from api.portal.serializers import active_enrollment
from api.serializers import sync_household_members


class _Rollback(Exception):
    """Sentinel to abort a dry-run's transaction without persisting."""


class Command(BaseCommand):
    help = "Reconcile household rosters with enrollment dietary profiles for all households."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client-id", type=str, action="append", default=None,
            help="Only the household(s) containing the given client id(s); repeatable.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing (rolls back).",
        )

    def handle(self, *args, **options):
        households = Household.objects.all()
        if options["client_id"]:
            households = households.filter(
                members__client_id__in=options["client_id"]
            ).distinct()

        households = households.prefetch_related("members__client")
        dry_run = options["dry_run"]

        total_households = 0
        touched_households = 0
        total_profiles = 0

        for household in households.iterator():
            total_households += 1
            # The active enrollment governing this household is the primary's
            # (falls back to any member's household enrollment).
            primary = (
                household.members.filter(is_primary=True).first()
                or household.members.first()
            )
            if primary is None or primary.client is None:
                continue
            client = primary.client
            enr = active_enrollment(client)
            if enr is None:
                continue

            if dry_run:
                # Reconcile inside a transaction, then roll it back so nothing
                # persists -- reports the counts without writing.
                created = 0
                try:
                    with transaction.atomic():
                        created = sync_household_members(client, enrollment=enr)
                        raise _Rollback()
                except _Rollback:
                    pass
            else:
                created = sync_household_members(client, enrollment=enr)

            if created:
                touched_households += 1
                total_profiles += created
                self.stdout.write(
                    f"  household {household.household_id}: "
                    f"+{created} profile(s) for {client.first_name} {client.last_name}"
                )

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Scanned {total_households} household(s); "
            f"{'would reconcile' if dry_run else 'reconciled'} {touched_households} "
            f"({total_profiles} dietary profile(s) {'to create' if dry_run else 'created'})."
        ))

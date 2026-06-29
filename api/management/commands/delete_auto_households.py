"""Delete the auto-created solo households (one per client) that the case import
generates via ``ensure_household_with_primary`` on every internal-service case.

Only removes households that are SAFE to remove:
  * exactly one member (solo), and
  * not attached to any EnrollmentVerification.

Deleting a Household cascades its HouseholdMember row(s) but never touches the
Client. Households that carry an enrollment are always kept.

NOTE: internal-service case saves recreate a solo household per client, so the
next Unite Us sync will regenerate these. Run this only when you want a clean
slate before (re)building family households.

Usage:
    python manage.py delete_auto_households                 # DRY RUN (no delete)
    python manage.py delete_auto_households --apply          # delete
    python manage.py delete_auto_households --created-after 2026-06-25 --apply
"""
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from api.models import Household


class Command(BaseCommand):
    help = (
        "Delete auto-created solo households (1 member, no enrollment). "
        "Dry-run unless --apply is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Perform the delete.")
        parser.add_argument(
            "--created-after",
            default="",
            help="Only delete households created on/after this date (YYYY-MM-DD).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        head = self.style.MIGRATE_HEADING

        qs = (
            Household.objects.annotate(n=Count("members"))
            .filter(n__lte=1, enrollment_verifications__isnull=True)
        )
        if options["created_after"]:
            try:
                d = datetime.strptime(options["created_after"], "%Y-%m-%d")
            except ValueError:
                self.stderr.write("Invalid --created-after; use YYYY-MM-DD.")
                return
            d = timezone.make_aware(d)
            qs = qs.filter(created_at__gte=d)

        total = Household.objects.count()
        with_enr = (
            Household.objects.filter(enrollment_verifications__isnull=False)
            .distinct()
            .count()
        )
        to_delete = qs.count()

        self.stdout.write(head("\n=== Delete auto-created solo households ==="))
        self.stdout.write(f"  total households            : {total}")
        self.stdout.write(f"  with an enrollment (KEEP)   : {with_enr}")
        self.stdout.write(f"  solo, no enrollment (DELETE): {to_delete}")

        if not apply:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: nothing deleted. Re-run with --apply.")
            )
            return

        with transaction.atomic():
            # Delete by explicit id set so the annotated/aggregated queryset
            # doesn't interfere with the cascade delete.
            ids = list(qs.values_list("household_id", flat=True))
            deleted, _ = Household.objects.filter(household_id__in=ids).delete()

        self.stdout.write(
            self.style.SUCCESS(f"\nDeleted {len(ids)} households ({deleted} rows total).")
        )
        self.stdout.write(f"households remaining: {Household.objects.count()}")

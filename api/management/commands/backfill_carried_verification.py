"""Backfill verification data onto live enrollments that lost it in a
governing-case replacement.

A household is verified once. When the governing internal-service case was
replaced by REUSING a pre-existing (unverified) enrollment, the old code closed
the verified enrollment and kept the pending one WITHOUT carrying the
verification fact forward (fixed in lifecycle._carry_verification_fields). So the
surviving live enrollment reached Service Active with a BLANK verified_by/at,
which makes the "Verified by" column empty and hides the member from the
"Verified by" filter's expected verifier.

This copies verified_at/verified_by (and requested_*/verified flags) from the
enrollment each live row SUPERSEDES (a closed, verified enrollment) onto the live
row -- but only when the live row isn't already verified (its own verification
wins). Dry-run by default; pass --apply to commit.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import _carry_verification_fields

TERMINAL = [EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED, EnrollmentStage.DISREGARDED]


class Command(BaseCommand):
    help = (
        "Carry verification data forward onto live enrollments that lost it in a "
        "governing-case replacement. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the backfill.")

    def handle(self, *args, **options):
        apply = options["apply"]
        qs = (
            EnrollmentVerification.objects.exclude(stage__in=TERMINAL)
            .filter(verified_at__isnull=True, supersedes__verified_at__isnull=False)
            .select_related("supersedes", "supersedes__verified_by")
        )
        total = qs.count()
        by_verifier = Counter()
        for vb in qs.values_list("supersedes__verified_by__name", flat=True):
            by_verifier[vb or "(unknown)"] += 1

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Backfill carried verification ==="))
        self.stdout.write(f"  live enrollments to fix: {total}")
        self.stdout.write("  by carried verifier (top 12):")
        for name, c in by_verifier.most_common(12):
            self.stdout.write(f"     {c:6}  {name}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing changed. Re-run with --apply."))
            return

        fixed = 0
        ids = list(qs.values_list("pk", flat=True))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            with transaction.atomic():
                for enr in (
                    EnrollmentVerification.objects.filter(pk__in=chunk)
                    .select_related("supersedes", "supersedes__verified_by")
                ):
                    before = enr.verified_at
                    _carry_verification_fields(enr, enr.supersedes)
                    enr.refresh_from_db(fields=["verified_at"])
                    if enr.verified_at is not None and before is None:
                        fixed += 1
        self.stdout.write(self.style.SUCCESS(f"\nAPPLIED: backfilled {fixed} enrollment(s)."))

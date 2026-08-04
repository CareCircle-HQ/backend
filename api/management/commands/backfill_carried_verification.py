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
from api.services.lifecycle import _carry_dietary_profiles, _carry_verification_fields

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
        # Every live (non-terminal) enrollment that supersedes a VERIFIED closed
        # enrollment: carry forward both the verification fact (when the survivor
        # isn't itself verified) AND the verified dietary config (blank fields).
        qs = (
            EnrollmentVerification.objects.exclude(stage__in=TERMINAL)
            .filter(supersedes__verified_at__isnull=False)
            .select_related("supersedes", "supersedes__verified_by")
        )
        total = qs.count()
        need_verif = qs.filter(verified_at__isnull=True).count()
        # Verified survivors still missing the captured delivery address (the FK
        # wasn't carried, only the verified flag) -- these read as "verified but
        # no delivery address".
        need_addr = qs.filter(
            delivery_address__isnull=True, supersedes__delivery_address__isnull=False
        ).count()
        by_verifier = Counter()
        for vb in qs.filter(verified_at__isnull=True).values_list(
            "supersedes__verified_by__name", flat=True
        ):
            by_verifier[vb or "(unknown)"] += 1

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Backfill carried verification ==="))
        self.stdout.write(f"  live enrollments superseding a verified one: {total}")
        self.stdout.write(f"    missing verified_at (verification carry): {need_verif}")
        self.stdout.write(f"    missing delivery address (address carry): {need_addr}")
        self.stdout.write("  by carried verifier (top 12):")
        for name, c in by_verifier.most_common(12):
            self.stdout.write(f"     {c:6}  {name}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing changed. Re-run with --apply."))
            return

        fixed = addr_fixed = dietary_fixed = 0
        ids = list(qs.values_list("pk", flat=True))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            with transaction.atomic():
                for enr in (
                    EnrollmentVerification.objects.filter(pk__in=chunk)
                    .select_related("supersedes", "supersedes__verified_by")
                ):
                    before_v = enr.verified_at
                    before_addr = enr.delivery_address_id
                    _carry_verification_fields(enr, enr.supersedes)
                    enr.refresh_from_db(fields=["verified_at", "delivery_address_id"])
                    if enr.verified_at is not None and before_v is None:
                        fixed += 1
                    if enr.delivery_address_id is not None and before_addr is None:
                        addr_fixed += 1
                    dietary_fixed += _carry_dietary_profiles(enr, enr.supersedes)
        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: verification carried onto {fixed} enrollment(s); "
            f"delivery address filled on {addr_fixed}; "
            f"dietary fields filled on {dietary_fixed} member profile(s)."
        ))

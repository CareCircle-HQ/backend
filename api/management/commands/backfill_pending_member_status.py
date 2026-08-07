"""Backfill the new PENDING member status onto pre-kitchen members.

Historically a MemberDietaryProfile defaulted to ACTIVE from creation, so members
who have NOT yet been through kitchen assignment (still in verification /
Nutritionist review) read as Active and could slip onto delivery schedules /
Purchase Orders. PENDING is now the correct pre-kitchen state (excluded from all
POs; promoted to Active only by the kitchen-assignment meal rule).

This converts ACTIVE profiles whose governing enrollment is still at a PRE-kitchen
stage to PENDING. It never touches members who genuinely reached service
(SERVICE_ACTIVE and beyond), nor any already-paused / off-ramped member.

Dry-run by default; pass --apply to commit.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    EnrollmentStage,
    MemberDietaryProfile,
    MemberStatus,
)

# Enrollment stages that sit BEFORE kitchen assignment activates a member. An
# ACTIVE profile on one of these never went through the meal rule, so it should
# be PENDING. SERVICE_ACTIVE / SERVICE_COMPLETE / CLOSED / CANCELLED / ON_HOLD are
# intentionally excluded (those members were genuinely activated / held).
_PRE_KITCHEN_STAGES = [
    EnrollmentStage.PENDING_VALIDATION,
    EnrollmentStage.VALIDATED,
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
]


class Command(BaseCommand):
    help = "Convert ACTIVE pre-kitchen member profiles to the new PENDING status. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = (
            MemberDietaryProfile.objects.filter(
                status=MemberStatus.ACTIVE,
                enrollment__stage__in=_PRE_KITCHEN_STAGES,
            )
            .select_related("enrollment")
        )
        total = qs.count()
        by_stage = Counter(
            qs.values_list("enrollment__stage", flat=True)
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Backfill PENDING member status ==="))
        self.stdout.write(f"  ACTIVE pre-kitchen profiles to convert: {total}")
        for stage, n in sorted(by_stage.items()):
            self.stdout.write(f"     {n:6}  {stage}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing changed. Re-run with --apply."))
            return

        updated = 0
        ids = list(qs.values_list("pk", flat=True))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            with transaction.atomic():
                updated += MemberDietaryProfile.objects.filter(pk__in=chunk).update(
                    status=MemberStatus.PENDING
                )
        self.stdout.write(self.style.SUCCESS(f"\nAPPLIED: set {updated} profile(s) to PENDING."))

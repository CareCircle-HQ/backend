"""Backfill dietary/clinical data onto member profiles that lost it across a
governing-case change.

When a governing-case change forks a new enrollment, every member's picture --
menu type, dietary restrictions, food allergies, other restrictions, verification
notes, meal category and service status -- must carry forward (it doesn't change
with the case/meal type). An older code path instead re-created carried members
as BLANK, PENDING placeholders on the new enrollment, so a member who was ACTIVE
with a full profile on the now-closed enrollment reappeared empty -- losing their
menu/dietary and silently dropping off deliveries (PENDING is excluded from POs).

``sync_household_members`` now carries the prior profile forward for any NEW
profile, so this only heals the EXISTING backlog: a member profile on a LIVE
enrollment that is still the blank placeholder (PENDING + empty menu) while the
same client has a richer profile on another (usually superseded) enrollment. We
copy the richest prior profile's fields forward and, when that reactivates a
member, rebuild the household calendar so they get occurrences again.

PO-committed dates are preserved by the rebuild. Dry-run by default.

Usage:
    python manage.py reconcile_carried_member_profiles            # dry run
    python manage.py reconcile_carried_member_profiles --apply
    python manage.py reconcile_carried_member_profiles --apply --limit 50
"""
from django.core.management.base import BaseCommand

from api.models import (
    EnrollmentStage,
    MemberDietaryProfile,
    MemberStatus,
)

_TERMINAL = [
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.DISREGARDED,
]

# INFORMATION fields carried forward from the member's prior profile (mirrors
# sync_household_members). Service STATUS is deliberately NOT carried -- it's
# governed by the scope rules (Household->Individual pauses extra members;
# Individual->Household re-activates them), so this backfill only restores the
# lost menu/dietary picture and never changes a member's active/paused state.
_CARRY_FIELDS = [
    "menu_type",
    "dietary_restrictions",
    "food_allergies",
    "other_dietary_restrictions",
    "meal_category",
    "general_verification_notes",
]


class Command(BaseCommand):
    help = (
        "Backfill menu/dietary/allergies/notes/status onto blank, PENDING member "
        "profiles on live enrollments from the same client's richer prior "
        "profile (lost across a governing-case change). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist changes.")
        parser.add_argument("--limit", type=int, default=0, help="Cap profiles processed.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        limit = opts["limit"]

        # Blank placeholders on a LIVE enrollment: PENDING + no menu chosen.
        placeholders = (
            MemberDietaryProfile.objects
            .exclude(enrollment__stage__in=_TERMINAL)
            .filter(status=MemberStatus.PENDING, menu_type="")
            .select_related("enrollment")
            .order_by("enrollment__client_id")
        )

        to_fix = []
        for mp in placeholders.iterator():
            prior = (
                MemberDietaryProfile.objects.filter(client_id=mp.client_id)
                .exclude(pk=mp.pk)
                .exclude(menu_type="")
                .order_by("-enrollment__opened_at")
                .first()
            )
            if prior is not None:
                to_fix.append((mp, prior))

        if limit:
            to_fix = to_fix[:limit]

        self.stdout.write(
            f"blank/pending live member profiles with a richer prior profile: "
            f"{len(to_fix)}"
        )
        for mp, prior in to_fix[:20]:
            self.stdout.write(
                f"  {mp.member_name or mp.client_id} (enr {mp.enrollment_id}) "
                f"<- prior enr {prior.enrollment_id}: menu={prior.menu_type!r} "
                f"status={prior.status}"
            )
        if len(to_fix) > 20:
            self.stdout.write(f"  ... and {len(to_fix) - 20} more")

        if not apply:
            self.stdout.write(self.style.WARNING("Dry run -- re-run with --apply."))
            return

        fixed = 0
        for mp, prior in to_fix:
            for f in _CARRY_FIELDS:
                setattr(mp, f, getattr(prior, f))
            mp.save(update_fields=_CARRY_FIELDS)
            fixed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfilled dietary info onto {fixed} member profile(s). "
                f"(Service status left to the scope rules.)"
            )
        )

"""Backfill ``verified_at`` on enrollments that were VERIFIED but never stamped.

The "Meal Inputs" import (and other early paths) advanced a household through the
funnel -- writing a ``verified`` StageEvent and moving the enrollment to Verified
/ Kitchen Assignment / Service Active -- but never set the ``verified_at``
TIMESTAMP on the enrollment itself. That left the enrollment SERVING while
carrying ``verified_at=None``.

The damage shows up on a governing-case change: ``_carry_service_and_activate``
has a HARD verification gate (``if not new_enr.verified_at: return False``), so a
case switch on such a household can't carry service forward and silently bounces
the WHOLE household back to Pending Verification (off every Purchase Order).

This heals the fact from the AUDIT LOG: any enrollment with ``verified_at`` NULL
that has a ``StageEvent`` proving it reached ``verified`` gets ``verified_at``
set to that event's time (the earliest such event). Evidence-based -- an
enrollment that never reached ``verified`` is left untouched. ``is_family_verified``
and ``delivery_address_verified`` are filled too (only when currently false), so
the record reads as a completed verification. ``verified_by`` stays null (a
system/import verification has no agent).

Dry-run by default.

Usage:
    python manage.py backfill_verified_at            # dry run
    python manage.py backfill_verified_at --apply
    python manage.py backfill_verified_at --apply --limit 100
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Min

from api.models import EnrollmentStage, EnrollmentVerification, StageEvent

# Only stamp enrollments CURRENTLY at or past Verified and still in the service
# pipeline. A reverted ``pending_verification`` (verification re-opened), a
# pre-verify stage, or a terminal (closed/cancelled/disregarded) row is left
# alone: stamping "verified" on a row that isn't currently verified would
# misrepresent its state, and terminal history is carried separately by
# ``backfill_carried_verification``.
_VERIFIED_OR_BEYOND = [
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.ON_HOLD,
]


class Command(BaseCommand):
    help = (
        "Stamp verified_at on enrollments that reached the Verified stage (per the "
        "StageEvent audit log) but never had the timestamp set -- so a later "
        "governing-case change carries service instead of bouncing the household "
        "back to Pending Verification. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist changes.")
        parser.add_argument("--limit", type=int, default=0, help="Cap enrollments processed.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        limit = opts["limit"]

        # Earliest ``verified`` StageEvent time per enrollment -- the evidence that
        # the household was verified, and the timestamp we stamp.
        verified_at_by_enr = dict(
            StageEvent.objects
            .filter(entity_type="enrollment", to_stage=EnrollmentStage.VERIFIED)
            .values_list("enrollment_id")
            .annotate(t=Min("entered_at"))
            .values_list("enrollment_id", "t")
        )

        # Enrollments missing the timestamp that DID reach verified (evidence)
        # and are CURRENTLY at or past Verified in the live pipeline.
        candidates = (
            EnrollmentVerification.objects
            .filter(
                verified_at__isnull=True,
                stage__in=[s.value for s in _VERIFIED_OR_BEYOND],
                pk__in=verified_at_by_enr.keys(),
            )
            .order_by("pk")
        )

        to_fix = [(e, verified_at_by_enr[e.pk]) for e in candidates]
        if limit:
            to_fix = to_fix[:limit]

        by_stage = Counter(e.stage for e, _ in to_fix)
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Backfill verified_at (reached Verified but never stamped) ==="
        ))
        self.stdout.write(f"  enrollments to stamp: {len(to_fix)}"
                          + (f"  (limited to {limit})" if limit else ""))
        for stage, n in sorted(by_stage.items()):
            self.stdout.write(f"     {n:6}  {stage}")
        for e, t in to_fix[:15]:
            self.stdout.write(f"    enr={e.pk} stage={e.stage} <- verified_at={t.isoformat()}")
        if len(to_fix) > 15:
            self.stdout.write(f"    ... and {len(to_fix) - 15} more")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDry run -- re-run with --apply."))
            return

        fixed = 0
        for e, t in to_fix:
            e.verified_at = t
            fields = ["verified_at"]
            if not e.is_family_verified:
                e.is_family_verified = True
                fields.append("is_family_verified")
            if not e.delivery_address_verified:
                e.delivery_address_verified = True
                fields.append("delivery_address_verified")
            e.save(update_fields=fields)
            fixed += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: stamped verified_at on {fixed} enrollment(s)."
        ))

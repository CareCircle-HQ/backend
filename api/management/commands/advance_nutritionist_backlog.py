"""One-time: advance households that are VERIFIED + already Nutritionist-approved
(grandfathered) AND whose governing authorization is already APPROVED to Kitchen
Assignment.

Before the Nutritionist gate existed, a verified + approved household advanced to
Kitchen Assignment automatically. The gate briefly blocked that; the grandfather
migrations then back-stamped these households as approved, but the stage advance
only re-fires on the next authorization reconcile. This nudges the already-approved
ones forward now. (Households approved LATER advance normally via the case
authorization reconcile, whose gate the grandfather stamp satisfies.)
"""

from django.core.management.base import BaseCommand

from api.models import (
    EnrollmentStage, EnrollmentVerification, ServiceAuthorizationStatus as A,
)
from api.services.lifecycle import (
    governing_internal_case, reconcile_enrollment_authorization,
)


class Command(BaseCommand):
    help = "Advance verified + nutritionist-approved + authorized households to Kitchen Assignment."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry run).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = EnrollmentVerification.objects.filter(
            stage=EnrollmentStage.VERIFIED,
            nutritionist_approved_at__isnull=False,
        ).select_related("case")
        moved = 0
        for enr in qs:
            gov = enr.case or governing_internal_case(enr)
            auth = getattr(gov, "service_authorization_status", "") if gov else ""
            if auth not in (A.APPROVED, A.NOT_REQUIRED):
                continue
            if apply:
                reconcile_enrollment_authorization(enr)
                enr.refresh_from_db()
                if EnrollmentStage(enr.stage) == EnrollmentStage.KITCHEN_ASSIGNMENT:
                    moved += 1
            else:
                moved += 1
        verb = "Advanced" if apply else "Would advance"
        self.stdout.write(f"{verb} {moved} household(s) to Kitchen Assignment.")

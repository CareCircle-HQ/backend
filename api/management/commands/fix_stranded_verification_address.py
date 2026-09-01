"""Carry a stranded verified delivery address back onto the governing enrollment.

Root cause (fixed in EnrollmentVerificationSerializer): a navigation/eligibility
case could bind a verification enrollment, so the verified delivery address landed
on that stray sibling row while the member's GOVERNING internal-service enrollment
stayed blank -- "verified, but no delivery address." The kitchen export silently
fell back to the client's profile Address, so service wasn't broken, but the
verified address (the source of truth) was on the wrong row and could diverge from
the profile address actually shipped to.

This finds members whose GOVERNING internal-service enrollment is verified with NO
delivery_address while a SIBLING enrollment has one, and copies the sibling's
delivery address (+ the delivery_address_verified flag) onto the governing
enrollment. Idempotent. Does NOT touch the sibling or any delivery calendar.

    python manage.py fix_stranded_verification_address            # dry run
    python manage.py fix_stranded_verification_address --apply
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Carry a stranded verified delivery address onto the governing "
        "internal-service enrollment. Dry-run by default; --apply to persist."
    )

    # Governing enrollment must be live (in the funnel or serving) to matter.
    def _live_stages(self):
        from api.models import EnrollmentStage
        return {
            EnrollmentStage.VERIFIED, EnrollmentStage.KITCHEN_ASSIGNMENT,
            EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.ON_HOLD,
        }

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Persist the carries (default: dry run).")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **opts):
        from api.models import Client, EnrollmentStage
        from api.portal import serializers as s

        live = self._live_stages()
        apply = opts["apply"]
        # Candidates: a verified enrollment with no delivery address.
        cand = Client.objects.filter(
            enrollments__verified_at__isnull=False,
            enrollments__delivery_address__isnull=True,
        ).distinct().prefetch_related("enrollments")

        carried, printed = 0, 0
        for c in cand:
            gov_case = s.internal_service_case(c)
            if gov_case is None:
                continue
            enrs = list(c.enrollments.all())
            gov_enr = next((e for e in enrs if e.case_id == gov_case.case_id), None)
            if (
                gov_enr is None
                or gov_enr.delivery_address_id is not None
                or gov_enr.verified_at is None
                or EnrollmentStage(gov_enr.stage) not in live
            ):
                continue
            # Sibling that DOES carry a delivery address -- prefer a verified one,
            # then the most recently opened.
            sibs = [
                e for e in enrs
                if e.pk != gov_enr.pk and e.delivery_address_id is not None
            ]
            if not sibs:
                continue
            sib = sorted(
                sibs,
                key=lambda e: (e.verified_at is not None, e.opened_at or e.pk),
                reverse=True,
            )[0]

            carried += 1
            if printed < opts["limit"]:
                printed += 1
                self.stdout.write(
                    f"  {'CARRY' if apply else 'would carry'} client {c.client_id} "
                    f"enr {gov_enr.pk} <- addr {sib.delivery_address_id} (from sibling enr {sib.pk})"
                )
            if apply:
                gov_enr.delivery_address_id = sib.delivery_address_id
                if not gov_enr.delivery_address_verified:
                    gov_enr.delivery_address_verified = (
                        sib.delivery_address_verified or True
                    )
                gov_enr.save(update_fields=[
                    "delivery_address", "delivery_address_verified",
                ])

        self.stdout.write("")
        self.stdout.write(
            ("APPLIED: " if apply else "DRY RUN (no changes): ")
            + f"carried {carried} stranded delivery address(es)."
        )
        if not apply:
            self.stdout.write("Re-run with --apply to persist.")

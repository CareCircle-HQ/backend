"""Re-home a member whose SERVING enrollment is stuck on a navigation case while
their governing internal-service enrollment is a hollow / out-of-orbit shell.

This is the INVERSE of resolve_double_serving_enrollments: here the NAV enrollment
holds the real service (active profile + live delivery plan) and the IS enrollment
is empty, so we must KEEP the nav enrollment (rebinding it onto the IS case) and
retire the hollow IS one -- retiring the nav one would strip service.

Steps (per --client), all inside one transaction on --apply:
  1. Rebind the serving nav enrollment's case -> the governing internal-service
     case (+ program_name / service_type from that case).
  2. Carry the delivery-type (apt/unit) address onto it.
  3. Carry the nutritionist approval from the hollow IS enrollment (keep serving).
  4. Retire the hollow IS enrollment (DISREGARDED).
  5. Rebuild the delivery calendar + recompute the client's stage.

Safety: aborts unless the client has EXACTLY one hollow IS enrollment (no plan)
and one serving nav enrollment (has a live plan). Dry-run by default.

    python manage.py rehome_serving_nav_enrollment --client <uuid>
    python manage.py rehome_serving_nav_enrollment --client <uuid> --apply
"""
from django.core.management.base import BaseCommand, CommandError

# Nutritionist-approval fields carried from the hollow IS enrollment onto the
# serving one so it keeps serving without a fresh review.
_NUTRI_FIELDS = (
    "nutritionist_approved_at",
    "nutritionist_signature",
    "nutritionist_signature_image",
    "nutritionist_approval_pdf_key",
)


class Command(BaseCommand):
    help = "Rehome a serving nav-case enrollment onto the governing IS case."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="client_id to fix.")
        parser.add_argument("--apply", action="store_true",
                            help="Persist (default: dry run).")

    def _has_live_plan(self, e):
        from django.utils import timezone
        today = timezone.localdate()
        return any(
            (p.ends_on is None or p.ends_on >= today)
            for p in e.delivery_schedules.all()
        )

    def handle(self, *args, **opts):
        from django.db import transaction
        from django.utils import timezone

        from api.models import Address, CaseType, Client, EnrollmentStage
        from api.portal import serializers as s
        from api.services.lifecycle import recompute_client_stage
        from api.services.orders import recompute_delivery_plan

        c = Client.objects.filter(pk=opts["client"]).first()
        if c is None:
            raise CommandError("client not found")
        gov_case = s.internal_service_case(c)
        if gov_case is None:
            raise CommandError("no governing internal-service case")

        enrs = list(c.enrollments.all())
        # Serving nav enrollment: on a navigation case, live stage, with a plan.
        serving = [
            e for e in enrs
            if e.case_id and e.case and e.case.case_type == CaseType.NAVIGATION
            and self._has_live_plan(e)
        ]
        # Hollow IS enrollment: on the governing IS case, no live plan.
        hollow = [
            e for e in enrs
            if e.case_id == gov_case.case_id and not self._has_live_plan(e)
        ]
        if len(serving) != 1 or len(hollow) != 1:
            raise CommandError(
                f"unexpected shape (serving_nav={[e.pk for e in serving]}, "
                f"hollow_is={[e.pk for e in hollow]}) -- aborting for safety."
            )
        serving = serving[0]
        hollow = hollow[0]

        # Delivery-type (apt) address to keep -- prefer the hollow IS enrollment's
        # if it's the type='delivery' one, else the serving one's own.
        hollow_addr = (Address.objects.filter(pk=hollow.delivery_address_id).first()
                       if hollow.delivery_address_id else None)
        keep_addr_id = serving.delivery_address_id
        if hollow_addr is not None and (hollow_addr.type or "").lower() == "delivery":
            keep_addr_id = hollow_addr.pk

        self.stdout.write(f"client {c.client_id} {c.first_name} {c.last_name}")
        self.stdout.write(
            f"  KEEP serving nav enr {serving.pk} -> rebind case "
            f"{str(serving.case_id)[:8]} => IS {str(gov_case.case_id)[:8]}"
        )
        self.stdout.write(
            f"  address: {serving.delivery_address_id} -> {keep_addr_id} "
            f"('{hollow_addr.street if hollow_addr else '?'}')"
        )
        self.stdout.write(
            f"  carry nutritionist approval from hollow enr {hollow.pk} "
            f"(approved {str(hollow.nutritionist_approved_at)[:10]})"
        )
        self.stdout.write(f"  RETIRE hollow IS enr {hollow.pk} -> DISREGARDED")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("Dry run -- re-run with --apply."))
            return

        with transaction.atomic():
            # 4 first: free the IS case so the rebind doesn't collide with a live row.
            hollow.stage = EnrollmentStage.DISREGARDED
            hollow.close_reason = "hollow_is_retired"
            hollow.stage_at = timezone.now()
            if hollow.closed_at is None:
                hollow.closed_at = timezone.now()
            hollow.save(update_fields=["stage", "close_reason", "stage_at", "closed_at"])

            # 1-3: rebind + address + nutri onto the serving enrollment.
            serving.case = gov_case
            serving.program_name = gov_case.program_name or serving.program_name
            serving.service_type = gov_case.service_type or serving.service_type
            if keep_addr_id:
                serving.delivery_address_id = keep_addr_id
                serving.delivery_address_verified = True
            for f in _NUTRI_FIELDS:
                if not getattr(serving, f, None) and getattr(hollow, f, None):
                    setattr(serving, f, getattr(hollow, f))
            serving.save()

            # 5: rebuild calendar + recompute stage.
            try:
                recompute_delivery_plan(serving)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  recompute_delivery_plan failed: {exc}")
            recompute_client_stage(c)

        self.stdout.write(self.style.SUCCESS(
            f"APPLIED: rehomed enr {serving.pk} onto IS case; retired hollow "
            f"enr {hollow.pk}."
        ))

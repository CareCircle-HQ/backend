"""Give a member their OWN enrollment (and calendar) on their OWN case.

For a member who holds an open, approved internal-service case but is served on a
RELATIVE's enrollment -- the shape behind the AKALLOO family:

  * each person has a separate Household record,
  * one enrollment carries them all as member profiles,
  * two of them each hold their own INDIVIDUAL-scope case, which by definition
    covers only its holder.

Two constraints make the order matter:

  1. a per-case unique index allows only ONE non-terminal enrollment per case, so
     the member's case must first be released by whoever currently holds it, and
  2. ``reopen_enrollment_for_new_case`` clones the prior roster, which on an
     individual-scope case would recreate the shared-enrollment problem. This
     command therefore creates the enrollment with EXACTLY the case holder on it.

Dry run by default; nothing is written without ``--apply``.

    python manage.py split_member_to_own_enrollment --client <uuid>
    python manage.py split_member_to_own_enrollment --client <uuid> --apply
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Give a member their own enrollment + calendar on their own case."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="Client UUID.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the changes. Omit for a dry run (rolled back).",
        )

    def handle(self, *args, **opts):
        from api.models import (
            Case, CaseType, Client, EnrollmentStage, EnrollmentVerification,
            MemberDietaryProfile, OrderStatus, ServiceAuthorizationStatus,
        )
        from api.services.delivery import (
            cadence_matching_weekdays, create_member_delivery_schedules,
        )
        from api.services.lifecycle import (
            replace_enrollment_for_case_change, _primary_enrollment,
        )
        from api.services.orders import rebuild_delivery_calendar

        client = Client.objects.filter(pk=opts["client"]).first()
        if client is None:
            raise CommandError(f"No client {opts['client']}")
        name = f"{client.first_name} {client.last_name}".strip()

        case = next(
            (
                c for c in Case.objects.filter(
                    client=client, case_type=CaseType.INTERNAL_SERVICE,
                    case_status="open",
                )
                if c.service_authorization_status in (
                    ServiceAuthorizationStatus.APPROVED,
                    ServiceAuthorizationStatus.NOT_REQUIRED,
                )
            ),
            None,
        )
        if case is None:
            raise CommandError(f"{name} holds no open, approved internal-service case")

        terminal = (EnrollmentStage.CLOSED.value, EnrollmentStage.CANCELLED.value)
        own_live = EnrollmentVerification.objects.filter(client=client).exclude(
            stage__in=terminal,
        ).first()
        if own_live is not None:
            raise CommandError(
                f"{name} already has a live enrollment ({own_live.pk}); nothing to do"
            )

        # The prior to carry verification / kitchen / cadence / address from.
        prior = (
            EnrollmentVerification.objects.filter(client=client, verified_at__isnull=False)
            .order_by("-opened_at", "-pk").first()
        )
        if prior is None:
            raise CommandError(
                f"{name} has no verified prior enrollment to carry data from -- "
                "run them through verification instead"
            )
        if not prior.kitchen_id or not (prior.delivery_weekdays or []):
            raise CommandError(
                f"{name}'s prior enrollment {prior.pk} has no kitchen/cadence to "
                "carry (kitchen=%s weekdays=%s) -- assign one in the UI instead"
                % (prior.kitchen_id, prior.delivery_weekdays)
            )
        cadence = cadence_matching_weekdays(prior.delivery_weekdays)
        if not cadence:
            raise CommandError(
                f"no cadence matches weekdays {prior.delivery_weekdays!r} -- an "
                "agent must choose one"
            )

        self.stdout.write(f"{name} ({client.pk})")
        self.stdout.write(f"  case      {case.case_id} scope={case.household_type}")
        self.stdout.write(
            f"  carrying  kitchen={prior.kitchen.name} weekdays={prior.delivery_weekdays} "
            f"cadence={cadence} (from enr {prior.pk})"
        )

        class _Rollback(Exception):
            pass

        try:
            with transaction.atomic():
                # 1. Release the case from whoever holds it live. Only its OWNER may
                #    move it (see _may_replace_enrollment), so this asks the holder's
                #    own governing case to reclaim their enrollment.
                holder = EnrollmentVerification.objects.filter(case=case).exclude(
                    stage__in=terminal,
                ).exclude(client=client).select_related("client").first()
                if holder is not None:
                    from api.portal.serializers import internal_service_case

                    holder_case = internal_service_case(holder.client)
                    moved = False
                    if holder_case is not None and str(holder_case.case_id) != str(case.case_id):
                        moved = bool(
                            replace_enrollment_for_case_change(holder.client, holder_case)
                        )
                    self.stdout.write(
                        f"  releasing case from enr {holder.pk} "
                        f"({holder.client.first_name}) -> moved={moved}"
                    )
                    if not moved:
                        raise CommandError(
                            f"could not release the case from enr {holder.pk}; "
                            "resolve that enrollment's case first"
                        )

                # 2. The member's own enrollment, carrying their verified state.
                enrollment = EnrollmentVerification.objects.create(
                    client=client, household=prior.household, case=case,
                    stage=EnrollmentStage.SERVICE_ACTIVE,
                    verified_at=prior.verified_at,
                    nutritionist_approved_at=prior.nutritionist_approved_at,
                    kitchen=prior.kitchen,
                    delivery_weekdays=list(prior.delivery_weekdays or []),
                    delivery_address=prior.delivery_address,
                    program_name=case.program_name or prior.program_name,
                )

                # 3. EXACTLY the case holder. An individual-scope case covers only
                #    them; copying the old roster is what created the shared
                #    enrollment in the first place.
                source_profile = prior.member_profiles.filter(client=client).first()
                MemberDietaryProfile.objects.create(
                    enrollment=enrollment, client=client, member_name=name,
                    status="active",
                    menu_type=(source_profile.menu_type if source_profile else ""),
                    food_allergies=(source_profile.food_allergies if source_profile else []),
                    dietary_restrictions=(
                        source_profile.dietary_restrictions if source_profile else []
                    ),
                )

                # 4. Plan + calendar.
                plans = create_member_delivery_schedules(
                    enrollment, case=case, cadence=cadence, kitchen=enrollment.kitchen,
                )
                result = rebuild_delivery_calendar(enrollment)
                occurrences = enrollment.orders.filter(
                    status=OrderStatus.SCHEDULED,
                ).count()

                self.stdout.write(
                    f"  created   enr {enrollment.pk} stage={enrollment.stage} "
                    f"members={[p.member_name for p in enrollment.member_profiles.all()]}"
                )
                self.stdout.write(
                    f"  calendar  plans={len(plans)} future_scheduled={occurrences} {result}"
                )
                if not opts["apply"]:
                    raise _Rollback
                self.stdout.write(self.style.SUCCESS("  APPLIED"))
        except _Rollback:
            self.stdout.write(self.style.WARNING("  DRY RUN -- rolled back, nothing written"))
            self.stdout.write("  re-run with --apply to write it")

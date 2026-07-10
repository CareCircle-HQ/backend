"""Give Out-of-Orbit members a delivery calendar so their profile shows the
"Out of Orbit" status on the dates they WOULD be served.

Out-of-orbit members (a menu/allergy combo the kitchen can't safely fulfill) are
created without a delivery plan, so they have no calendar at all. This command
sweeps every non-terminal Out-of-Orbit member that has no plan of their own and,
when their household has a REAL delivery plan for another member, mirrors that
plan (cadence / kitchen / product / authorization window) onto a plan for the
Out-of-Orbit member, then rebuilds the calendar. The occurrences are then
overlaid "Out of Orbit" by the delivery-calendar view and remain excluded from
Purchase Orders (live member-status filter).

Members whose household has NO plan to mirror (no kitchen / cadence chosen yet)
are reported, not fabricated -- there is no real schedule to base dates on.

Idempotent. Dry-run unless ``--apply``.

Usage:
    python manage.py generate_out_of_orbit_calendars              # DRY RUN
    python manage.py generate_out_of_orbit_calendars --apply       # commit
    python manage.py generate_out_of_orbit_calendars --limit 10
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    EnrollmentStage,
    HouseholdMember,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    MemberStatus,
    ScheduleStatus,
)
from api.services.orders import sync_delivery_calendar

_TERMINAL_STAGES = (
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
)

# Fields mirrored from the household's template plan onto the Out-of-Orbit plan.
_MIRROR_FIELDS = (
    "program_id", "product_type_id", "kitchen_id", "delivery_days_cadence",
    "prod_per_delivery", "meals_per_day", "meals_boxes_total", "starts_on", "ends_on",
)


class Command(BaseCommand):
    help = (
        "Generate a mirrored delivery calendar (shown 'Out of Orbit') for "
        "Out-of-Orbit members whose household has a real plan; report the rest. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N members.")

    def handle(self, *args, **options):
        apply = options["apply"]

        members = list(
            MemberDietaryProfile.objects.filter(status=MemberStatus.OUT_OF_ORBIT)
            .exclude(enrollment__stage__in=_TERMINAL_STAGES)
            .select_related("enrollment", "client")
        )
        # Only those without a plan of their own.
        todo = [
            mv for mv in members
            if not MemberDeliverySchedule.objects.filter(
                enrollment=mv.enrollment, member_profile=mv,
                status=ScheduleStatus.SCHEDULED,
            ).exists()
        ]
        if options["limit"]:
            todo = todo[: options["limit"]]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nOut-of-Orbit members (non-terminal) without a plan: {len(todo)}"
        ))

        report = Counter()
        occurrences_added = 0
        case_b = []  # (client_id, reason)
        enr_to_sync = set()

        with transaction.atomic():
            for mv in todo:
                template = (
                    MemberDeliverySchedule.objects.filter(
                        enrollment=mv.enrollment, status=ScheduleStatus.SCHEDULED,
                    ).exclude(member_profile=mv).first()
                )
                if template is None:
                    report["case_b"] += 1
                    reason = (
                        "no kitchen assigned"
                        if mv.enrollment.kitchen_id is None
                        else "no household delivery plan"
                    )
                    case_b.append((str(mv.client_id), reason))
                    continue
                self._mirror_plan(mv, template)
                report["plans_created"] += 1
                enr_to_sync.add(mv.enrollment_id)

            # Rebuild each affected enrollment's calendar once; the overlay then
            # labels the new occurrences "Out of Orbit".
            from api.models import EnrollmentVerification

            for enr in EnrollmentVerification.objects.filter(pk__in=enr_to_sync):
                res = sync_delivery_calendar(enr)
                occurrences_added += res["added"]

            self._report(report, occurrences_added, case_b, apply)

            if not apply:
                transaction.set_rollback(True)

    def _mirror_plan(self, mv, template):
        """Create (or revive) a SCHEDULED plan for ``mv`` mirroring ``template``,
        using the member's own menu type / name. Kitchen meal output stays blank
        (they're Out of Orbit -- not actually fulfilled)."""
        c = getattr(mv, "client", None)
        member_name = (
            f"{getattr(c, 'first_name', '')} {getattr(c, 'last_name', '')}".strip()
            or mv.member_name
        )
        household_member = (
            HouseholdMember.objects.filter(client_id=mv.client_id).first()
            if mv.client_id else None
        )
        plan = MemberDeliverySchedule.objects.filter(
            enrollment=mv.enrollment, member_profile=mv,
        ).first() or MemberDeliverySchedule(
            enrollment=mv.enrollment, member_profile=mv,
        )
        for f in _MIRROR_FIELDS:
            setattr(plan, f, getattr(template, f))
        plan.household_member = household_member
        plan.member_name = member_name
        plan.menu_type = mv.menu_type or template.menu_type
        plan.kitchen_meal_type = ""
        plan.kitchen_food_notes = ""
        plan.status = ScheduleStatus.SCHEDULED
        plan.save()
        return plan

    def _report(self, report, occurrences_added, case_b, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Out-of-Orbit calendar generation ==="))
        self.stdout.write(f"  {'Plans created (Case A)':<38}: {report.get('plans_created', 0)}")
        self.stdout.write(f"  {'Calendar occurrences added':<38}: {occurrences_added}")
        self.stdout.write(f"  {'Skipped -- no plan to mirror (Case B)':<38}: {report.get('case_b', 0)}")

        if case_b:
            by_reason = Counter(r for _, r in case_b)
            self.stdout.write(head("\nCase B breakdown (needs kitchen/plan first):"))
            for reason, n in by_reason.most_common():
                self.stdout.write(f"  [{n}] {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

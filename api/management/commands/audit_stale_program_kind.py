"""Find enrollments whose stored program kind (Meals/Boxes) disagrees with their
GOVERNING internal-service case -- the stale-``program_name`` bug that hides
switched members from the correct Purchase Order.

Background: when a household's governing case switches Meals<->Boxes, the
governing pointer + effective kind update, but ``enrollment.program_name`` (and
the dated ``OrderSchedule`` snapshots it seeds) can stay on the OLD program.
Because PO kind-resolution trusts the schedule's ``program_name`` first, those
members get classified as the wrong kind and dropped from the right PO.

This command flags every enrollment with FUTURE ``SCHEDULED`` occurrences where
the governing case's DETECTED kind differs from the enrollment snapshot kind or
any future scheduled occurrence's kind.

Read-only: makes no changes.

Usage:
    python manage.py audit_stale_program_kind
    python manage.py audit_stale_program_kind --limit 50
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import EnrollmentVerification, OrderSchedule, ScheduleStatus
from api.services.catalog import (
    detected_product_kind_for_enrollment,
    product_type_kind_for_name,
)


class Command(BaseCommand):
    help = "List enrollments whose program kind disagrees with the governing case."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Max mismatched enrollments to print (0 = all).",
        )

    def handle(self, *args, **opts):
        today = timezone.localdate()
        limit = opts.get("limit") or 0
        w = self.stdout.write

        enr_ids = (
            OrderSchedule.objects.filter(
                status=ScheduleStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
            )
            .values_list("enrollment_id", flat=True)
            .distinct()
        )
        enr_ids = [e for e in enr_ids if e is not None]
        w(f"Enrollments with future SCHEDULED occurrences: {len(enr_ids)}")

        mismatched = []
        for enr in EnrollmentVerification.objects.filter(pk__in=enr_ids).select_related(
            "client"
        ):
            gov = detected_product_kind_for_enrollment(enr)
            if gov is None:
                continue  # can't determine the governing kind -> skip
            snap = product_type_kind_for_name(enr.program_name)
            sched_rows = OrderSchedule.objects.filter(
                enrollment_id=enr.pk,
                status=ScheduleStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
            ).values_list("program_name", flat=True)
            sched_kinds = {product_type_kind_for_name(p) for p in sched_rows}
            snap_bad = snap is not None and snap != gov
            sched_bad = any(k is not None and k != gov for k in sched_kinds)
            if snap_bad or sched_bad:
                mismatched.append((enr, gov, snap, sched_kinds))

        w(self.style.MIGRATE_HEADING(f"\nMismatched enrollments: {len(mismatched)}"))
        shown = mismatched if limit <= 0 else mismatched[:limit]
        for enr, gov, snap, sched_kinds in shown:
            gov_l = getattr(gov, "value", gov)
            snap_l = getattr(snap, "value", snap)
            sk = sorted(getattr(k, "value", k) for k in sched_kinds if k is not None)
            w(
                f"  enr={enr.pk} client={enr.client_id} stage={enr.stage} "
                f"| governing={gov_l} snapshot={snap_l} schedule_kinds={sk} "
                f"| program_name={enr.program_name!r}"
            )
        if limit and len(mismatched) > limit:
            w(f"  ... and {len(mismatched) - limit} more (raise --limit).")

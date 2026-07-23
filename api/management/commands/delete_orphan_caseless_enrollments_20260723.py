"""One-off data fix (2026-07-23): delete truly-orphaned caseless enrollments.

An ``EnrollmentVerification`` is a meal/box enrollment. It should only exist for
a client who has an INTERNAL-SERVICE (meal/box) case. The CSV import created a
batch of enrollments whose ``case`` FK is null AND whose client has **no
internal-service case at all** -- these clients only ever had
navigation/eligibility cases (or none), so they should never have had a meal/box
enrollment. They are safe to remove.

STRICT, conservative target (exactly the "20 true orphans" from the audit):

    enrollment.case IS NULL
    AND the client has NO internal-service case of any status
    AND the client has NO other case-linked enrollment (so this isn't the stray
        half of a duplicate whose sibling holds the real case)

This deliberately EXCLUDES the ~2,472 caseless PRIMARY enrollments whose client
DOES have an internal-service case (those must be fixed by LINKING the case, not
deleted) and every duplicate whose sibling is case-linked.

Deleting an enrollment CASCADES to its MemberDietaryProfile / MemberDeliverySchedule
/ OrderSchedule / Process / StageEvent rows (TimelineEvent + Warning are SET_NULL).
Already-generated DeliveryOrders reference the client, not the enrollment, so they
are NOT cascade-deleted.

Dry-run by default (prints what WOULD be deleted + cascade counts, writes a CSV).
Pass --apply to commit. Each candidate is re-checked inside the transaction, so a
client who gained an internal-service case since the scan is skipped. Idempotent.
"""
import csv

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    Case,
    CaseType,
    EnrollmentVerification,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    OrderSchedule,
    StageEvent,
)


def _isc_client_ids():
    return set(
        Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
        .values_list("client_id", flat=True)
    )


def _case_linked_client_ids():
    return set(
        EnrollmentVerification.objects.filter(case__isnull=False)
        .values_list("client_id", flat=True)
    )


def _orphan_enrollments():
    """Caseless enrollments whose client has no internal-service case and no
    other case-linked enrollment. Returns a list of EnrollmentVerification."""
    isc = _isc_client_ids()
    cased = _case_linked_client_ids()
    qs = (
        EnrollmentVerification.objects.filter(case__isnull=True)
        .exclude(client_id__in=cased)
        .select_related("client")
    )
    return [e for e in qs if e.client_id not in isc]


def _client_has_isc(client_id):
    return Case.objects.filter(
        case_type=CaseType.INTERNAL_SERVICE, client_id=client_id
    ).exists()


def _cascade_counts(enrollment):
    return {
        "profiles": MemberDietaryProfile.objects.filter(enrollment=enrollment).count(),
        "schedules": MemberDeliverySchedule.objects.filter(enrollment=enrollment).count(),
        "orders": OrderSchedule.objects.filter(enrollment=enrollment).count(),
        "stage_events": StageEvent.objects.filter(enrollment=enrollment).count(),
    }


class Command(BaseCommand):
    help = (
        "Delete truly-orphaned caseless enrollments (client has NO internal-"
        "service case). Dry-run by default; --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without this the command only previews.",
        )
        parser.add_argument(
            "--csv", default="",
            help="Optional path to write the full candidate report.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        orphans = _orphan_enrollments()

        totals = {"profiles": 0, "schedules": 0, "orders": 0, "stage_events": 0}
        rows = []
        for e in orphans:
            c = e.client
            cc = _cascade_counts(e)
            for k in totals:
                totals[k] += cc[k]
            rows.append((e, c, cc))

        self.stdout.write(
            f"Found {len(orphans)} truly-orphaned caseless enrollment(s)."
        )
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nWould delete (enrollment | client | stage | cascade prof/sched/orders/events):"))
        for e, c, cc in rows:
            name = f"{c.first_name} {c.last_name}".strip() if c else "?"
            self.stdout.write(
                f"  enr {e.pk} | {name} ({str(e.client_id)[:8]}) | {e.stage} | "
                f"{cc['profiles']}/{cc['schedules']}/{cc['orders']}/{cc['stage_events']}"
            )
        self.stdout.write(
            f"\nCascade totals -- MemberDietaryProfile: {totals['profiles']}, "
            f"MemberDeliverySchedule: {totals['schedules']}, "
            f"OrderSchedule: {totals['orders']}, StageEvent: {totals['stage_events']}."
        )

        if opts["csv"]:
            with open(opts["csv"], "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow([
                    "enrollment_id", "client_id", "client_name", "stage",
                    "member_profiles", "delivery_schedules", "order_schedules",
                    "stage_events", "program_name",
                ])
                for e, c, cc in rows:
                    w.writerow([
                        e.pk, e.client_id,
                        f"{c.first_name} {c.last_name}".strip() if c else "",
                        e.stage, cc["profiles"], cc["schedules"], cc["orders"],
                        cc["stage_events"], (e.program_name or "")[:80],
                    ])
            self.stdout.write(self.style.SUCCESS(f"\nWrote report to {opts['csv']}"))

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run -- no changes made. Re-run with --apply to delete."
            ))
            return

        deleted = 0
        skipped = 0
        with transaction.atomic():
            for e, c, cc in rows:
                # Re-verify inside the transaction: never delete an enrollment
                # whose client gained an internal-service case since the scan.
                if e.case_id is not None or _client_has_isc(e.client_id):
                    skipped += 1
                    continue
                e.delete()
                deleted += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied: deleted {deleted} orphaned enrollment(s)"
            + (f", skipped {skipped} (gained a case)." if skipped else ".")
        ))

"""Enforce Delivery Coverage across every household with an out-of-range ZIP.

Sweeps all non-terminal enrollments that have at least one member whose DELIVERY
address or PRIMARY (Current/Home) address ZIP is in the editable
:class:`ExcludedZipCode` table, and applies the app's standard out-of-range
handling to each (via ``_enforce_delivery_coverage``):

  * sets every non-terminal member of the household **Out of Range** (with a
    system note + timeline event);
  * opens a High-severity **Case Closure** ticket (flags the Unite Us case for
    an agent to close), pre-filled with the offending ZIP;
  * places the whole household **On Hold**.

Delivery occurrences are intentionally KEPT: once members are Out of Range and
the household is On Hold, the delivery calendar overlays that status on future
dates and Purchase Order generation excludes them via the live status/stage
filters -- so scheduled deliveries carry the right status without deleting
history.

Idempotent: a member already Out of Range / a household already On Hold / an
existing open out-of-range ticket are all skipped. Dry-run unless ``--apply``.

Usage:
    python manage.py enforce_out_of_range              # DRY RUN
    python manage.py enforce_out_of_range --apply       # commit
    python manage.py enforce_out_of_range --limit 10    # first 10 households
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    MemberDietaryProfile,
    Ticket,
    TicketStatus,
    TicketTypeCode,
)
from api.services.service_area import excluded_zips, member_excluded_info

# Terminal stages: service already ended, so nothing to enforce/hold.
_TERMINAL_STAGES = (
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
)


class Command(BaseCommand):
    help = (
        "Set members Out of Range, open a Case Closure ticket, and hold the "
        "household for every non-terminal enrollment with an excluded-ZIP "
        "address. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N households.")

    def handle(self, *args, **options):
        # Import here to avoid pulling DRF view modules at command registration.
        from api.portal.views_members import _enforce_delivery_coverage

        apply = options["apply"]
        excluded = excluded_zips()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nExcluded ZIPs configured: {len(excluded)} -> {sorted(excluded)}"
        ))
        if not excluded:
            self.stdout.write(self.style.WARNING("No excluded ZIPs configured -- nothing to do."))
            return

        # Candidate enrollments: any non-terminal enrollment with >=1 member whose
        # delivery/primary ZIP is excluded. member_excluded_info checks the
        # enrollment delivery address + the member's primary address.
        profiles = (
            MemberDietaryProfile.objects
            .exclude(enrollment__stage__in=_TERMINAL_STAGES)
            .select_related("enrollment", "enrollment__delivery_address", "client")
            .prefetch_related("client__addresses")
        )
        enr_ids = set()
        affected_members = 0
        for mv in profiles.iterator(chunk_size=1000):
            zip_code, _src = member_excluded_info(mv, excluded=excluded)
            if zip_code:
                affected_members += 1
                if mv.enrollment_id:
                    enr_ids.add(mv.enrollment_id)

        enr_ids = list(enr_ids)
        if options["limit"]:
            enr_ids = enr_ids[: options["limit"]]

        self.stdout.write(
            f"  Members currently in an excluded ZIP: {affected_members}"
        )
        self.stdout.write(f"  Households (enrollments) to enforce: {len(enr_ids)}\n")

        report = Counter()
        oor_members = 0

        with transaction.atomic():
            tickets_before = self._open_closure_tickets()
            held_before = EnrollmentVerification.objects.filter(
                pk__in=enr_ids, stage=EnrollmentStage.ON_HOLD
            ).count()

            for enr_id in enr_ids:
                try:
                    with transaction.atomic():
                        enr = EnrollmentVerification.objects.get(pk=enr_id)
                        res = _enforce_delivery_coverage(enr, None)
                    n = len(res.get("out_of_range", []))
                    oor_members += n
                    report["households_processed"] += 1
                    if n:
                        report["households_with_new_oor"] += 1
                except Exception as exc:  # isolate a bad household, keep going
                    report["errors"] += 1
                    self.stdout.write(self.style.ERROR(f"  enr {enr_id}: {exc}"))

            tickets_after = self._open_closure_tickets()
            held_after = EnrollmentVerification.objects.filter(
                pk__in=enr_ids, stage=EnrollmentStage.ON_HOLD
            ).count()

            self._report(report, oor_members, tickets_after - tickets_before,
                         held_after - held_before, apply)

            if not apply:
                transaction.set_rollback(True)

    @staticmethod
    def _open_closure_tickets():
        return Ticket.objects.filter(
            type__code=TicketTypeCode.CASE_CLOSURE
        ).exclude(status=TicketStatus.RESOLVED).count()

    def _report(self, report, oor_members, tickets_opened, households_held, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Out-of-range enforcement ==="))
        self.stdout.write(f"  {'Households processed':<34}: {report.get('households_processed', 0)}")
        self.stdout.write(f"  {'Households w/ members set OOR':<34}: {report.get('households_with_new_oor', 0)}")
        self.stdout.write(f"  {'Members set Out of Range':<34}: {oor_members}")
        self.stdout.write(f"  {'Case Closure tickets opened':<34}: {tickets_opened}")
        self.stdout.write(f"  {'Households newly placed On Hold':<34}: {households_held}")
        self.stdout.write(f"  {'Errored':<34}: {report.get('errors', 0)}")
        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

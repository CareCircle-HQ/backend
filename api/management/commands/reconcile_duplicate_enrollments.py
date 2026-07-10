"""Reconcile clients who have DUPLICATE active delivery enrollments.

A data anomaly leaves some clients with two enrollments that BOTH carry a live
delivery plan (e.g. a real, case-linked verification plus a spurious caseless
enrollment created by a bulk path). Each active enrollment builds its own
delivery calendar, so the client lands twice on every delivery date and gets
duplicated in Purchase Orders.

For each client with two-or-more plan-bearing enrollments this command:
  * picks the KEEPER -- the plan-bearing enrollment linked to the client's
    governing internal-service case,
  * CLOSES every other plan-bearing enrollment via
    ``close_duplicate_enrollment`` (cancels its plans, drops its future
    non-batched occurrences, sets it CANCELLED).

Occurrences already committed to a DeliveryOrder are preserved. Clients where no
plan-bearing enrollment sits on the governing case are SKIPPED (ambiguous ->
needs human review), so service is never removed from a client by mistake.

Dry-run by default (rolls back). Re-runnable and idempotent.

Usage:
    python manage.py reconcile_duplicate_enrollments            # DRY RUN
    python manage.py reconcile_duplicate_enrollments --apply     # commit
    python manage.py reconcile_duplicate_enrollments --limit 5   # first 5 clients
"""
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    EnrollmentVerification,
    MemberDeliverySchedule,
    ScheduleStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
)
from api.services.lifecycle import governing_internal_case
from api.services.orders import close_duplicate_enrollment


class Command(BaseCommand):
    help = (
        "Close spurious duplicate enrollments for clients who have two "
        "plan-bearing enrollments (keeps the one on the governing internal "
        "case). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="Process first N clients.")

    def handle(self, *args, **options):
        apply = options["apply"]

        # Enrollments that carry a live (SCHEDULED) delivery plan and aren't in an
        # excluded stage -- the ones that actually build a calendar / feed POs.
        plan_enr_ids = set(
            MemberDeliverySchedule.objects.filter(status=ScheduleStatus.SCHEDULED)
            .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
            .values_list("enrollment_id", flat=True)
        )
        enrs = (
            EnrollmentVerification.objects.filter(pk__in=plan_enr_ids)
            .select_related("client")
        )
        by_client = defaultdict(list)
        for e in enrs:
            by_client[e.client_id] = by_client[e.client_id] + [e]

        dup_clients = {c: es for c, es in by_client.items() if len(es) > 1}
        client_ids = list(dup_clients.keys())
        if options["limit"]:
            client_ids = client_ids[: options["limit"]]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nClients with >1 plan-bearing enrollment: {len(dup_clients)}"
        ))

        report = Counter()
        closed_detail = []   # (client_id, kept_enr, closed_enr)
        skipped = []         # (client_id, reason)

        with transaction.atomic():
            for cid in client_ids:
                es = dup_clients[cid]
                try:
                    with transaction.atomic():
                        outcome = self._process(cid, es, closed_detail, skipped)
                except Exception as exc:  # isolate a bad client, keep going
                    outcome = "error"
                    skipped.append((cid, str(exc)))
                report[outcome] += 1

            if not apply:
                transaction.set_rollback(True)

        self._report(report, closed_detail, skipped, apply)

    def _process(self, cid, enrollments, closed_detail, skipped):
        gov = governing_internal_case(enrollments[0])
        if gov is None:
            skipped.append((cid, "no governing internal-service case"))
            return "skipped"
        keepers = [e for e in enrollments if str(e.case_id) == str(gov.case_id)]
        if not keepers:
            skipped.append((cid, "no plan-bearing enrollment on the governing case"))
            return "skipped"
        keeper = keepers[0]
        duplicates = [e for e in enrollments if e.pk != keeper.pk]
        for dup in duplicates:
            close_duplicate_enrollment(dup)
            closed_detail.append((str(cid), keeper.pk, dup.pk))
        return "reconciled"

    def _report(self, report, closed_detail, skipped, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Duplicate enrollment reconciliation ==="))
        self.stdout.write(f"  {'Clients reconciled':<40}: {report.get('reconciled', 0)}")
        self.stdout.write(f"  {'Duplicate enrollments closed':<40}: {len(closed_detail)}")
        self.stdout.write(f"  {'Clients skipped (needs review)':<40}: {report.get('skipped', 0)}")
        self.stdout.write(f"  {'Errored':<40}: {report.get('error', 0)}")

        if closed_detail:
            self.stdout.write(head(f"\nClosed (showing up to 30):"))
            for client_id, kept, closed in closed_detail[:30]:
                self.stdout.write(f"  client {client_id}: kept enr {kept}, closed enr {closed}")
        if skipped:
            self.stdout.write(head(f"\nSkipped ({len(skipped)}, showing up to 30):"))
            for client_id, reason in skipped[:30]:
                self.stdout.write(f"  client {client_id}: {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

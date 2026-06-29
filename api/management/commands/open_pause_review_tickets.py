"""Open a "Pause Service" review ticket for every household currently On Hold,
so paused members surface in the work queue for follow-up (resume or close).

Background: ``pause_services`` and the manual Hold action only record a client
Note + StageEvent when a household is paused -- no work-queue ticket -- so a
paused member can sit On Hold indefinitely with nothing prompting a review.
This backfill opens one ``PAUSE_SERVICE`` ticket per on-hold enrollment, linked
to the member (client) and their case.

Idempotent: ``open_ticket`` dedupes on (type, client, case, reason), so
re-running never creates duplicates. Dry-run unless ``--apply``.

Usage:
    python manage.py open_pause_review_tickets            # dry-run (no writes)
    python manage.py open_pause_review_tickets --apply    # commit
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    TicketSeverity,
    TicketTypeCode,
)
from api.services.tickets import open_ticket

_REASON = (
    "This member's service is On Hold (paused). Review the hold and decide "
    "whether to resume or close the service."
)


class Command(BaseCommand):
    help = (
        "Open a 'Pause Service' review ticket for every On Hold household "
        "(linked to the member + case). Idempotent; dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--reason",
            default=_REASON,
            help="Reason text recorded on each ticket (also the dedupe key).",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Process only the first N enrollments."
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        reason = (opts["reason"] or _REASON).strip()
        qs = (
            EnrollmentVerification.objects.filter(stage=EnrollmentStage.ON_HOLD)
            .select_related("client", "case")
            .order_by("id")
        )
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        report = Counter()

        def _run():
            for enr in qs.iterator():
                if enr.client_id is None:
                    report["skipped_no_client"] += 1
                    continue
                _, created = open_ticket(
                    TicketTypeCode.PAUSE_SERVICE,
                    reason=reason,
                    severity=TicketSeverity.MEDIUM,
                    client=enr.client,
                    case=enr.case,
                )
                report["created" if created else "already_exists"] += 1

        if apply:
            with transaction.atomic():
                _run()
        else:
            # Dry-run: open the tickets in a transaction, count them, roll back.
            class _Rollback(Exception):
                pass

            try:
                with transaction.atomic():
                    _run()
                    raise _Rollback()
            except _Rollback:
                pass

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Pause-review tickets ==="))
        self.stdout.write(f"  On-hold enrollments scanned : {sum(report.values())}")
        self.stdout.write(f"  Tickets created             : {report.get('created', 0)}")
        self.stdout.write(f"  Already had an open ticket  : {report.get('already_exists', 0)}")
        if report.get("skipped_no_client"):
            self.stdout.write(
                f"  Skipped (no client link)    : {report['skipped_no_client']}"
            )
        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

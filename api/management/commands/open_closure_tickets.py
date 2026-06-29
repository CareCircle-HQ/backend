"""Open a "Case Closure" follow-up ticket for each client/case listed in a
"Pending Closure" sheet (Unite Us export).

Sheet columns:
    A = NEEDED ACTION (e.g. "Pending Closure")
    B = Internal Service Case ID (Unite Us)
    C = Client ID (Unite Us)

For each row we create one High-severity ticket of type ``Case Closure``,
source ``Other``, linked to the member (client) and the case, with a reason
asking an agent to review the member for service closure.

Idempotent: a row that already has an open/in-progress Case Closure ticket for
the same (client, case) is skipped, so re-running won't pile up duplicates.

Dry-run unless ``--apply`` (rolls back so you can review the report first).

Usage:
    python manage.py open_closure_tickets --file ~/data/PendingClosure.xlsx
    python manage.py open_closure_tickets --file ~/data/PendingClosure.xlsx --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    Case,
    Client,
    Ticket,
    TicketSeverity,
    TicketSource,
    TicketStatus,
    TicketType,
    TicketTypeCode,
)
from api.management.commands.import_meal_verifications import _clean, _read_rows

_COL_CASE, _COL_CLIENT = "B", "C"
_OPEN_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
_DEFAULT_REASON = (
    "Case is pending closure (per the Unite Us 'Pending Closure' list). Review "
    "the member's meal/box service and complete the closure process, confirming "
    "the end of deliveries with the member and recording the closure reason."
)


class Command(BaseCommand):
    help = (
        "Open a High-severity 'Case Closure' ticket (source Other) for each "
        "client/case in a 'Pending Closure' sheet. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--reason", default=_DEFAULT_REASON, help="Ticket reason/description."
        )

    def handle(self, *args, **options):
        rows = _read_rows(options["file"])
        reason = (options["reason"] or _DEFAULT_REASON).strip()
        apply = options["apply"]

        # Case Closure ticket type (seeded from TicketTypeCode; create on the fly
        # if the table hasn't been seeded yet).
        type_obj, _ = TicketType.objects.get_or_create(
            code=TicketTypeCode.CASE_CLOSURE,
            defaults={"label": TicketTypeCode.CASE_CLOSURE.label},
        )

        report = Counter()
        flags = []  # (client_id, note) for skips / partial links

        with transaction.atomic():
            for cells in rows:
                client_id = _clean(cells.get(_COL_CLIENT))
                try:
                    with transaction.atomic():
                        outcome = self._open_row(cells, type_obj, reason)
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                if outcome[0] not in ("created", "already_open"):
                    flags.append((client_id, outcome[1] if len(outcome) > 1 else ""))
                elif len(outcome) > 1 and outcome[1]:
                    flags.append((client_id, outcome[1]))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, flags, apply)

    def _open_row(self, cells, type_obj, reason):
        client_id = _clean(cells.get(_COL_CLIENT))
        case_id = _clean(cells.get(_COL_CASE))

        client = Client.objects.filter(client_id=client_id).first() if client_id else None
        case = Case.objects.filter(pk=case_id).first() if case_id else None

        if client is None and case is None:
            return ("not_found", "neither client nor case in DB")

        # Idempotent: reuse an existing open Case Closure ticket for this subject.
        existing = Ticket.objects.filter(
            type=type_obj, status__in=_OPEN_STATUSES, client=client, case=case
        ).first()
        if existing:
            return ("already_open",)

        Ticket.objects.create(
            type=type_obj,
            severity=TicketSeverity.HIGH,
            source=TicketSource.OTHER,
            reason=reason,
            client=client,
            case=case,
        )
        # Surface partial links so they can be chased up.
        if client is None:
            return ("created", "client id not in DB (linked case only)")
        if case is None:
            return ("created", "case id not in DB (linked member only)")
        return ("created",)

    def _report(self, report, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Open Case Closure tickets ==="))
        order = [
            ("created", "Tickets created"),
            ("already_open", "Skipped: ticket already open"),
            ("not_found", "Skipped: client + case not in DB"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<40}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<40}: {sum(report.values())}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 40):"))
            for cid, why in flags[:40]:
                self.stdout.write(f"  {cid or '(blank)'}: {why}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

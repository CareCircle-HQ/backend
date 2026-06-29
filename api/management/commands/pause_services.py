"""Pause (move to On Hold) household services for the clients/cases listed in a
"Services Paused" sheet (Unite Us export).

Sheet columns:
    A = NEEDED ACTION (e.g. "Services Paused")
    B = Internal Service Case ID (Unite Us)
    C = Client ID (Unite Us)

For each row we locate the household enrollment -- by the case id first (the
sheet is case-scoped), else the client's active enrollment -- and move it to
On Hold via :func:`advance_enrollment`. That pauses the WHOLE household (logs a
StageEvent + timeline entry and excludes it from new Purchase Orders) and is
idempotent: rows already On Hold are skipped. A client Note records the reason,
mirroring the manual Hold action in the portal.

Dry-run unless ``--apply`` (rolls back so you can review the report first).

Usage:
    python manage.py pause_services --file ~/data/ServicesPaused.xlsx
    python manage.py pause_services --file ~/data/ServicesPaused.xlsx --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    Note,
    NoteSource,
)
from api.management.commands.import_meal_verifications import _clean, _read_rows
from api.portal.serializers import active_enrollment
from api.services.lifecycle import InvalidTransition, advance_enrollment

_COL_CASE, _COL_CLIENT = "B", "C"
_DEFAULT_REASON = "Services paused per Unite Us cases list."


class Command(BaseCommand):
    help = (
        "Move household enrollments to On Hold for the clients/cases listed in a "
        "'Services Paused' sheet. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--reason",
            default=_DEFAULT_REASON,
            help="Reason recorded on the hold (StageEvent note + client Note).",
        )

    def handle(self, *args, **options):
        rows = _read_rows(options["file"])
        reason = (options["reason"] or _DEFAULT_REASON).strip()
        apply = options["apply"]

        report = Counter()
        flags = []  # (client_id, reason) for anything not paused/already-held

        with transaction.atomic():
            for cells in rows:
                client_id = _clean(cells.get(_COL_CLIENT))
                try:
                    with transaction.atomic():
                        outcome = self._pause_row(cells, reason)
                except Exception as exc:  # isolate a bad row, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                if outcome[0] not in ("paused", "already_on_hold"):
                    flags.append((client_id, outcome[1] if len(outcome) > 1 else ""))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, flags, apply)

    def _pause_row(self, cells, reason):
        client_id = _clean(cells.get(_COL_CLIENT))
        case_id = _clean(cells.get(_COL_CASE))

        # Prefer the case-scoped enrollment (the sheet lists cases); fall back to
        # the client's active enrollment.
        enr = None
        if case_id:
            enr = (
                EnrollmentVerification.objects.filter(case__case_id=case_id)
                .order_by("-opened_at")
                .first()
            )
        client = Client.objects.filter(client_id=client_id).first() if client_id else None
        if enr is None and client is not None:
            enr = active_enrollment(client)

        if enr is None:
            if client is None and not case_id:
                return ("skip_no_id", "row has no client/case id")
            if client is None:
                return ("client_not_found", "client id not in DB")
            return ("no_enrollment", "no enrollment to pause")

        if EnrollmentStage(enr.stage) == EnrollmentStage.ON_HOLD:
            return ("already_on_hold",)

        try:
            advance_enrollment(
                enr, EnrollmentStage.ON_HOLD, note=f"Bulk pause: {reason}"
            )
        except InvalidTransition:
            return ("cannot_pause", f"'{enr.stage}' -> on_hold not allowed")

        if enr.client_id:
            Note.objects.create(
                client=enr.client,
                source=NoteSource.AGENT,
                author_name="services-paused import",
                body=f"Service placed on hold. Reason: {reason}",
            )
        return ("paused",)

    def _report(self, report, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Pause services ==="))
        order = [
            ("paused", "Paused (moved to On Hold)"),
            ("already_on_hold", "Skipped: already On Hold"),
            ("no_enrollment", "Skipped: no enrollment to pause"),
            ("cannot_pause", "Skipped: terminal stage (can't hold)"),
            ("client_not_found", "Skipped: client id not in DB"),
            ("skip_no_id", "Skipped: blank client/case id"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<40}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<40}: {sum(report.values())}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}, showing up to 35):"))
            for cid, why in flags[:35]:
                self.stdout.write(f"  {cid or '(blank)'}: {why}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )

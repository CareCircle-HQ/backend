"""Backfill the delivery window for Williamsburg clients stuck Service Active
with an EMPTY calendar, then rebuild their schedule + orders so they become
ready to serve.

Root cause: the Williamsburg exception fast-tracks a client to Service Active
WITHOUT a service authorization, but the delivery-calendar builder derives its
date range from the case's authorization APPROVAL window
(``service_authorization_approval_starts_at`` / ``_ends_at``). When that window
is blank the calendar is empty (0 OrderSchedules). These cases almost always DO
carry the case's own requested window (``service_authorization_request_*``), so
we use THAT as the service window -- i.e. the window "depends on the internal
service case" itself, not an arbitrary default.

Per affected client (latest enrollment, Service Active, 0 calendar orders):
  1. If the case has no approval window but HAS a request window, copy the
     request window into the approval window (the case's own dates).
  2. Clear the empty delivery schedules + orders and rebuild them
     (create_member_delivery_schedules -> generate_delivery_calendar) so the
     calendar spans the now-present window.
  3. Report the new order count. Clients whose case has NEITHER an approval nor
     a request window (nor any usable date) are reported as unfixable.

Scope: all ``is_williamsburg`` clients, or just the ids in ``--file``.
Dry-run (no writes) unless ``--apply``.

Usage:
    python manage.py williamsburg_fix_case_window                 # dry run
    python manage.py williamsburg_fix_case_window --apply         # commit
    python manage.py williamsburg_fix_case_window --file list.xlsx --apply
"""
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Client, DeliveryCadence, EnrollmentStage


def _norm(value):
    return "" if value is None else str(value).strip()


def _read_ids(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    header = [_norm(c) for c in rows[0]]
    try:
        ci = header.index("Unite Us Client ID")
    except ValueError:
        ci = 0
    seen, out = set(), []
    for r in rows[1:]:
        cid = _norm(r[ci]).lower() if ci < len(r) else ""
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


class Command(BaseCommand):
    help = (
        "Backfill the service window (from the case's requested authorization "
        "dates) for Williamsburg clients that are Service Active with an empty "
        "calendar, and rebuild their schedule + orders. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default="", help="Optional .xlsx to scope to.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]

        if path:
            ids = _read_ids(path)
            clients = [Client.objects.filter(client_id=cid).first() for cid in ids]
            clients = [c for c in clients if c is not None]
            source = f"{path} ({len(clients)} of {len(ids)} ids in DB)"
        else:
            clients = list(Client.objects.filter(is_williamsburg=True))
            source = f"all is_williamsburg clients ({len(clients)})"

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Williamsburg fix-case-window: {source}"
        ))

        report = Counter()
        fixed = []      # (cid, note)
        unfixable = []  # (cid, note)

        with transaction.atomic():
            for client in clients:
                cid = str(client.client_id)
                enr = client.enrollments.order_by("-opened_at").first()
                if enr is None or enr.stage != EnrollmentStage.SERVICE_ACTIVE:
                    continue
                if enr.orders.exists():
                    report["already_ready"] += 1
                    continue

                try:
                    with transaction.atomic():
                        bucket, note = self._fix(enr)
                except Exception as exc:  # isolate a bad row
                    bucket, note = ("error", str(exc))

                report[bucket] += 1
                if bucket == "fixed":
                    fixed.append((cid, note))
                elif bucket in ("unfixable", "error"):
                    unfixable.append((cid, f"[{bucket}] {note}"))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, fixed, unfixable, apply, len(clients))

    def _fix(self, enr):
        """Set the approval window from the requested window (if needed), then
        rebuild schedules + calendar. Returns (bucket, note)."""
        from api.services.delivery import create_member_delivery_schedules
        from api.services.orders import generate_delivery_calendar

        case = enr.case
        if case is None:
            return ("unfixable", "enrollment has no linked case")

        have_approval = bool(
            case.service_authorization_approval_starts_at
            and case.service_authorization_approval_ends_at
        )
        have_request = bool(
            case.service_authorization_request_starts_at
            and case.service_authorization_request_ends_at
        )

        window_note = ""
        if not have_approval:
            if not have_request:
                return ("unfixable", "case has no approval AND no request window")
            # Use the case's OWN requested window as the service window.
            case.service_authorization_approval_starts_at = (
                case.service_authorization_request_starts_at
            )
            case.service_authorization_approval_ends_at = (
                case.service_authorization_request_ends_at
            )
            case.save(update_fields=[
                "service_authorization_approval_starts_at",
                "service_authorization_approval_ends_at",
            ])
            window_note = (
                f"window set from request "
                f"{case.service_authorization_approval_starts_at.date()} -> "
                f"{case.service_authorization_approval_ends_at.date()}; "
            )

        # Rebuild: the empty schedules were built off the missing window, and the
        # calendar reads dates from the schedule rows, so both must be recreated.
        enr.orders.all().delete()
        enr.delivery_schedules.all().delete()
        create_member_delivery_schedules(
            enr, case=case, cadence=DeliveryCadence.MON_THU, kitchen=enr.kitchen,
        )
        generate_delivery_calendar(enr)

        n_orders = enr.orders.count()
        if n_orders == 0:
            return ("unfixable", window_note + "still 0 orders after rebuild "
                    "(check cadence / member profiles / window range)")
        return ("fixed", window_note + f"rebuilt {enr.delivery_schedules.count()} "
                f"schedule(s) + {n_orders} order(s)")

    def _report(self, report, fixed, unfixable, apply, total):
        head = self.style.MIGRATE_HEADING

        self.stdout.write(self.style.SUCCESS(
            f"\n=== Fixed (window set + calendar rebuilt) -- {len(fixed)} ==="
        ))
        for cid, note in fixed:
            self.stdout.write(f"  {cid}: {note}")

        if unfixable:
            self.stdout.write(self.style.ERROR(
                f"\n=== Could NOT fix -- {len(unfixable)} ==="
            ))
            for cid, note in unfixable:
                self.stdout.write(f"  {cid}: {note}")

        self.stdout.write(head("\n=== Stats ==="))
        stats = [
            ("Clients scanned", total),
            ("Already ready (had orders)", report["already_ready"]),
            ("Fixed", report["fixed"]),
            ("Unfixable (no window / still empty)", report["unfixable"]),
            ("Errored", report["error"]),
        ]
        for label, value in stats:
            self.stdout.write(f"  {label:<38}: {value}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: no changes written. Re-run with --apply to commit."
            ))

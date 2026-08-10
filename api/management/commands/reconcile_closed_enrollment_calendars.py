"""Clear stale delivery occurrences off CLOSED / CANCELLED enrollments, then
rebuild each affected client's LIVE enrollment calendar.

A terminated enrollment must never keep live (SCHEDULED) future delivery
occurrences: they don't feed any Purchase Order (terminal enrollments are
excluded), and -- before the dedupe fix -- they blocked the client's LIVE
survivor from building its own near-term dates, stranding the member off the PO
(e.g. a re-kitchened household whose survivor only had far-future occurrences).

For every terminal enrollment that still holds future SCHEDULED occurrences this:
  1. truncates them (shortens the dead plan window + drops future non-batched
     occurrences via sync), then
  2. rebuilds the client's LIVE enrollment calendar so the freed near-term dates
     are regenerated on the enrollment that actually serves.

PO-committed (batched) dates are always preserved. Dry-run by default.

Usage:
    python manage.py reconcile_closed_enrollment_calendars           # dry run
    python manage.py reconcile_closed_enrollment_calendars --apply
    python manage.py reconcile_closed_enrollment_calendars --apply --limit 50
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    OrderSchedule,
    OrderStatus,
)
from api.services.orders import rebuild_delivery_calendar, truncate_future_deliveries

_TERMINAL = [EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED]


class Command(BaseCommand):
    help = (
        "Truncate stale future delivery occurrences on CLOSED/CANCELLED "
        "enrollments and rebuild each affected client's live enrollment calendar. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N terminal enrollments this run (0 = no limit).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        limit = opts["limit"]
        today = timezone.localdate()

        # Terminal enrollments that still hold a future SCHEDULED occurrence.
        # NB: clear ordering before .distinct() -- OrderSchedule has default
        # ordering, which would otherwise make .distinct() dedupe on
        # (order_cols, enrollment_id) and over-count.
        stale_enr_ids = sorted(
            OrderSchedule.objects
            .filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                enrollment__stage__in=[s.value for s in _TERMINAL],
            )
            .order_by()
            .values_list("enrollment_id", flat=True)
            .distinct()
        )
        if limit:
            stale_enr_ids = stale_enr_ids[:limit]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Reconcile closed-enrollment calendars ==="
        ))
        self.stdout.write(
            f"  terminal enrollments holding future occurrences: {len(stale_enr_ids)}"
            + (f"  (limited to {limit})" if limit else "")
        )

        terminal = EnrollmentVerification.objects.filter(pk__in=stale_enr_ids)
        client_ids = set(
            terminal.exclude(client__isnull=True).values_list("client_id", flat=True)
        )

        if not apply:
            by_stage = Counter(terminal.values_list("stage", flat=True))
            for stage, n in sorted(by_stage.items()):
                self.stdout.write(f"     {n:6}  {stage}")
            self.stdout.write(
                f"  affected clients (live enrollment to rebuild): {len(client_ids)}"
            )
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: nothing changed. Re-run with --apply."
            ))
            return

        removed_total = 0
        for enr in terminal.iterator():
            try:
                res = truncate_future_deliveries(enr)
                removed_total += (res or {}).get("removed", 0)
            except Exception:  # noqa: BLE001 - never abort the sweep
                self.stderr.write(f"  truncate failed for enrollment {enr.pk}")

        rebuilt = 0
        added_total = 0
        for cid in client_ids:
            live = (
                EnrollmentVerification.objects
                .filter(client_id=cid)
                .exclude(stage__in=[s.value for s in _TERMINAL])
                .order_by("-stage_at").first()
            )
            if live is None:
                continue
            try:
                res = rebuild_delivery_calendar(live)
                added_total += (res or {}).get("added", 0)
                rebuilt += 1
            except Exception:  # noqa: BLE001
                self.stderr.write(f"  rebuild failed for client {cid} (enr {live.pk})")

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: truncated {len(stale_enr_ids)} terminal enrollment(s) "
            f"(removed {removed_total} stale occurrence(s)); rebuilt {rebuilt} live "
            f"enrollment(s) (added {added_total} occurrence(s))."
        ))

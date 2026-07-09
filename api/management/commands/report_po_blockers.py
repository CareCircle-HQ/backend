"""Find members who SHOULD be in Purchase Orders but are blocked (read-only).

A member is expected to appear in POs when they have a live delivery PLAN
(``MemberDeliverySchedule`` status SCHEDULED), an active member profile, a
non-excluded enrollment stage, and an assigned kitchen. That plan is expanded
into dated ``OrderSchedule`` occurrences, which PO generation then filters by
product kind. A member can silently drop out at any of those steps.

This command walks every active plan and classifies each member by the reason
they can't reach a live PO line, so the whole CLASS of "active member missing
from every PO" can be recognized at once (the same situation that hit JACKIE
MARIN) instead of chasing them one by one.

Reasons (one per member, first that applies):

* ``no_kitchen``            -- enrollment has no assigned kitchen.
* ``lapsed_window_fixable`` -- 0 future occurrences, plan window elapsed, but the
                               governing authorization is approved with a FUTURE
                               end date -> fix with ``backfill_delivery_calendar``.
* ``needs_reauth``          -- 0 future occurrences and no future/approved
                               authorization -> re-authorize or off-board.
* ``no_future_generated``   -- 0 future occurrences though the window looks
                               valid -> a plain ``sync_delivery_calendars`` should
                               regenerate them.
* ``kind_unresolved``       -- has future occurrences but the product kind can't
                               be resolved (meal/box) -> the PO preview drops them.
* ``stale_case_link``       -- eligible AND resolvable, but enrollment.case does
                               not point at the governing internal-service case
                               (data hygiene; not blocking after the preview fix).
* ``ok``                    -- has future occurrences with a resolvable kind
                               (not reported unless --show-ok).

Read-only. Usage::

    python manage.py report_po_blockers
    python manage.py report_po_blockers --csv tmp/po_blockers.csv
    python manage.py report_po_blockers --reason lapsed_window_fixable
"""
import csv as csvmod
from collections import Counter

from django.core.management.base import BaseCommand

from api.services.po_blockers import (
    REASON_ORDER,
    classify_po_blockers,
    summarize_po_blockers,
)

_CSV_FIELDS = [
    "reason", "client_id", "member_name", "enrollment_id", "stage",
    "kitchen_id", "plan_ends_on", "future_occurrences",
    "governing_case_id", "governing_program", "auth_status", "auth_window_end",
    "enrollment_case_id", "program_name", "kind",
]


class Command(BaseCommand):
    help = (
        "Report members who should be in Purchase Orders but are blocked, "
        "bucketed by cause. Read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="csv_path", default=None,
                            help="Write full per-member rows to this CSV path.")
        parser.add_argument("--reason", dest="reason", default=None,
                            help="Only print members with this reason.")
        parser.add_argument("--show-ok", action="store_true",
                            help="Include members that are fine (reason=ok).")
        parser.add_argument("--limit", type=int, default=40,
                            help="Max detail rows to print (default 40).")

    def handle(self, *args, **opts):
        rows = classify_po_blockers(include_ok=opts["show_ok"])
        counts = Counter(summarize_po_blockers(rows))

        self._print_summary(counts)

        report_rows = list(rows)
        if opts["reason"]:
            report_rows = [r for r in report_rows if r["reason"] == opts["reason"]]
        report_rows.sort(key=lambda r: REASON_ORDER.index(r["reason"])
                         if r["reason"] in REASON_ORDER else len(REASON_ORDER))

        self._print_detail(report_rows, opts["limit"])

        if opts["csv_path"]:
            self._write_csv(opts["csv_path"], report_rows)
            self.stdout.write(self.style.SUCCESS(
                f"\nWrote {len(report_rows)} rows to {opts['csv_path']}"
            ))

    # -- output -------------------------------------------------------------
    def _print_summary(self, counts):
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== PO blocker summary ==="))
        total = sum(counts.values())
        for reason in REASON_ORDER:
            if reason in counts:
                blocked = reason != "ok"
                style = self.style.WARNING if blocked else self.style.SUCCESS
                self.stdout.write(style(f"  {reason:24} {counts[reason]}"))
        self.stdout.write(f"  {'total (excl. ok unless --show-ok)':34} {total}")

    def _print_detail(self, rows, limit):
        if not rows:
            return
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== detail (blocked members) ==="))
        for r in rows[:limit]:
            self.stdout.write(
                f"  [{r['reason']}] {r['member_name']} ({r['client_id']}) "
                f"enr {r['enrollment_id']} | kitchen {r['kitchen_id'] or '-'} | "
                f"future {r['future_occurrences']} | plan_ends {r['plan_ends_on'] or '-'} | "
                f"auth {r['auth_status']} end {r['auth_window_end'] or '-'}"
            )
        if len(rows) > limit:
            self.stdout.write(f"  ... {len(rows) - limit} more (use --csv for the full list)")

    def _write_csv(self, path, rows):
        with open(path, "w", newline="") as fh:
            w = csvmod.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

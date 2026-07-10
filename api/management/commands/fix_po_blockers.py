"""Bulk-apply the PO Blockers one-click fix across every flagged enrollment.

Runs the SAME server-side remediation as the PO Blockers "Fix" button
(:func:`api.services.po_blockers.remediate_enrollment_blocker`) for every
enrollment classified with a fixable reason -- so the whole backlog (e.g. ~6k
"stale case link" rows) can be cleared in one shot instead of clicking each row.

For ``stale_case_link`` this repoints the enrollment to its governing
internal-service case (or, when the case is already owned by another serving
enrollment, closes the spurious duplicate). Ambiguous cases are reported as
"needs review" and left untouched -- only the ones that CAN be fixed change.

Dry-run by default (rolls back). Re-runnable and idempotent.

Usage:
    python manage.py fix_po_blockers                              # DRY RUN, stale_case_link
    python manage.py fix_po_blockers --apply                       # commit
    python manage.py fix_po_blockers --reason all --apply          # every fixable reason
    python manage.py fix_po_blockers --reason cadence_weekday_mismatch --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import EnrollmentVerification
from api.services.po_blockers import (
    FIXABLE_REASONS,
    classify_po_blockers,
    remediate_enrollment_blocker,
)


class Command(BaseCommand):
    help = (
        "Bulk-apply the PO Blockers fix to every flagged enrollment for the "
        "given reason(s). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--reason", default="stale_case_link",
            help="Reason to fix, or 'all' for every fixable reason. "
                 f"Fixable: {', '.join(sorted(FIXABLE_REASONS))}.",
        )
        parser.add_argument("--limit", type=int, default=0, help="Process first N enrollments.")

    def handle(self, *args, **options):
        apply = options["apply"]
        reason_arg = options["reason"].strip().lower()
        if reason_arg == "all":
            target_reasons = set(FIXABLE_REASONS)
        elif reason_arg in FIXABLE_REASONS:
            target_reasons = {reason_arg}
        else:
            raise CommandError(
                f"Reason {reason_arg!r} is not fixable. "
                f"Choose from: {', '.join(sorted(FIXABLE_REASONS))}, or 'all'."
            )

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nClassifying PO blockers (targeting: {', '.join(sorted(target_reasons))})..."
        ))
        rows = classify_po_blockers()
        # One remediation per enrollment (a household with N members emits N rows
        # for the same enrollment-level reason).
        seen = set()
        targets = []  # (enrollment_id, reason)
        for r in rows:
            if r["reason"] in target_reasons and r["enrollment_id"] not in seen:
                seen.add(r["enrollment_id"])
                targets.append((r["enrollment_id"], r["reason"]))
        if options["limit"]:
            targets = targets[: options["limit"]]

        self.stdout.write(f"  Enrollments flagged for these reason(s): {len(targets)}")

        report = Counter()
        actions = Counter()
        not_fixed = []  # (enrollment_id, message)

        with transaction.atomic():
            for enr_id, reason in targets:
                try:
                    with transaction.atomic():
                        enr = EnrollmentVerification.objects.get(pk=enr_id)
                        res = remediate_enrollment_blocker(enr, reason)
                    if res.get("fixed"):
                        report["fixed"] += 1
                        actions[res.get("action", "?")] += 1
                    else:
                        report["not_fixed"] += 1
                        not_fixed.append((enr_id, res.get("message", "")))
                except Exception as exc:  # isolate a bad row, keep going
                    report["error"] += 1
                    not_fixed.append((enr_id, f"ERROR: {exc}"))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, actions, not_fixed, apply)

    def _report(self, report, actions, not_fixed, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== PO Blockers bulk fix ==="))
        self.stdout.write(f"  {'Fixed':<34}: {report.get('fixed', 0)}")
        for action, n in sorted(actions.items()):
            self.stdout.write(f"    - {action:<30}: {n}")
        self.stdout.write(f"  {'Not fixed (needs review)':<34}: {report.get('not_fixed', 0)}")
        self.stdout.write(f"  {'Errored':<34}: {report.get('error', 0)}")

        if not_fixed:
            # Group by message so the review buckets are obvious.
            by_msg = Counter(msg for _, msg in not_fixed)
            self.stdout.write(head("\nNeeds review (by reason):"))
            for msg, n in by_msg.most_common():
                self.stdout.write(f"  [{n}] {msg[:110]}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

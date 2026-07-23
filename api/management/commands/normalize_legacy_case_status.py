"""Normalize legacy cases stored with a raw Unite Us status.

Case status is Open/Closed ONLY (driven by the closed date) -- the importers and
the browser extension already map the raw Unite Us state to open/closed. But
older imports persisted raw values (``managed``, ``pending_authorization``), so
some rows still read "Managed" / "Pending Authorization" in the UI even though
those are not real case-status states (authorization is a SEPARATE dimension).

This re-derives those rows the SAME way the importers do, so it is safe:
  * legacy status WITH a ``case_closed_at``  -> ``closed``
  * legacy status WITHOUT a ``case_closed_at`` -> ``open``

Re-deriving (rather than a blanket -> open) matters: a genuinely-closed case
still labelled with a legacy status must become ``closed`` -- flipping it to
``open`` would keep its members Purchase-Order-eligible (the exact closed-case
leak the PO guardrail prevents), just relabelled.

DRY-RUN BY DEFAULT: prints the counts and makes NO changes. Pass ``--apply`` to
write.

Usage:
    python manage.py normalize_legacy_case_status            # dry-run
    python manage.py normalize_legacy_case_status --apply     # mutate
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, CaseStatus

# Raw Unite Us statuses the importers never emit -- case_status is open/closed
# only, so these legacy values are re-derived from the closed date.
LEGACY_STATUSES = (
    CaseStatus.MANAGED,
    CaseStatus.PENDING_AUTHORIZATION,
    CaseStatus.OFF_PLATFORM,
    CaseStatus.DRAFT,
)


class Command(BaseCommand):
    help = "Re-derive legacy case_status values (managed/pending_authorization/off_platform/draft) to open/closed (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write the normalized statuses (mutates data).",
        )

    def handle(self, *args, **opts):
        apply = opts.get("apply", False)
        head = self.style.MIGRATE_HEADING
        mode = self.style.ERROR("APPLY (mutating)") if apply else self.style.SUCCESS("DRY-RUN")

        self.stdout.write(head(f"normalize_legacy_case_status: {mode}\n"))

        total_closed = total_open = 0
        for st in LEGACY_STATUSES:
            qs = Case.objects.filter(case_status=st)
            t = qs.count()
            c = qs.filter(case_closed_at__isnull=False).count()
            total_closed += c
            total_open += t - c
            self.stdout.write(f"  {st.value:24} total={t:6}  ->closed={c:5}  ->open={t - c:6}")
        self.stdout.write(f"\n  TOTAL -> closed: {total_closed}")
        self.stdout.write(f"  TOTAL -> open:   {total_open}")

        if not apply:
            self.stdout.write("\nRe-run with --apply to write the changes.")
            return

        with transaction.atomic():
            # Closed first, then open, each re-filtered on the legacy values so
            # the two updates never overlap.
            closed_n = Case.objects.filter(
                case_status__in=LEGACY_STATUSES, case_closed_at__isnull=False
            ).update(case_status=CaseStatus.CLOSED)
            open_n = Case.objects.filter(
                case_status__in=LEGACY_STATUSES, case_closed_at__isnull=True
            ).update(case_status=CaseStatus.OPEN)

        self.stdout.write(
            self.style.WARNING(f"\nUpdated {closed_n} -> closed, {open_n} -> open.")
        )
        self.stdout.write(head("Done."))
        if closed_n:
            self.stdout.write(
                "Note: cases flipped to CLOSED may leave members with an active "
                "enrollment. Run `stop_closed_case_service --all --apply` (or wait "
                "for the nightly sweep) to cancel their service."
            )

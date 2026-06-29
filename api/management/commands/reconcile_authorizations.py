"""Backfill: project each verified household's CASE authorization outcome onto
its enrollment stage -- the step the nightly Unite Us import normally performs
(``DailyPull._reconcile_enrollments`` -> ``reconcile_enrollment_authorization``).

Why this exists: enrollments that reached ``verified`` (or ``waiting_authorization``)
while their linked case was later approved never advanced to ``kitchen_assignment``
because the reconcile pass didn't run for them (e.g. the daily pull was failing on
expired Unite Us credentials, or the rows were bulk-imported without a reconcile).
This command re-runs that idempotent projection for every eligible enrollment.

It funnels through the SAME chokepoint as every other path
(``reconcile_enrollment_authorization``), so it only ever acts on enrollments that
are past verification:

    Approved / Not required -> Kitchen Assignment
    Pending / (blank)       -> Waiting Authorization
    Denied                  -> Denied
    Expired                 -> On Hold

It NEVER touches ``pending_verification`` enrollments (those are not in
``_AUTH_ELIGIBLE_STAGES``), so the pending-verification queue is left intact.

Dry-run unless ``--apply`` so you can review the transition counts first.

Usage:
    python manage.py reconcile_authorizations            # dry-run (no writes)
    python manage.py reconcile_authorizations --apply    # commit
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import reconcile_enrollment_authorization

# Only enrollments past verification are reconciled (mirrors
# api.services.lifecycle._AUTH_ELIGIBLE_STAGES). pending_verification is
# deliberately excluded. DENIED is included so a denial superseded by a newer
# (re-)approved internal-service case gets moved forward again.
ELIGIBLE_STAGES = [
    EnrollmentStage.VERIFIED,
    EnrollmentStage.WAITING_AUTHORIZATION,
    EnrollmentStage.DENIED,
]


class Command(BaseCommand):
    help = (
        "Project case authorization onto verified/waiting enrollments "
        "(Approved -> Kitchen Assignment). Idempotent; never touches "
        "pending_verification. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--limit", type=int, default=0, help="Process only the first N enrollments."
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = (
            EnrollmentVerification.objects.filter(stage__in=ELIGIBLE_STAGES)
            .select_related("case")
            .order_by("id")
        )
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        transitions = Counter()
        scanned = 0
        changed = 0

        def _process():
            nonlocal scanned, changed
            for enr in qs.iterator():
                scanned += 1
                before = enr.stage
                try:
                    reconcile_enrollment_authorization(enr)
                except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
                    self.stderr.write(f"  enrollment {enr.pk}: {exc}")
                    continue
                after = EnrollmentVerification.objects.get(pk=enr.pk).stage
                if after != before:
                    changed += 1
                    transitions[f"{before} -> {after}"] += 1

        if apply:
            _process()
        else:
            # Dry-run: do the real projection inside a transaction, read back the
            # (uncommitted) results, then roll the whole thing back.
            class _Rollback(Exception):
                pass

            try:
                with transaction.atomic():
                    _process()
                    raise _Rollback()
            except _Rollback:
                pass

        verb = "changed" if apply else "would change"
        self.stdout.write(
            f"Scanned {scanned} eligible enrollments; {changed} {verb}."
        )
        for transition, n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {transition}: {n}")
        if not apply:
            self.stdout.write(
                self.style.WARNING("DRY RUN - nothing persisted. Re-run with --apply to commit.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("Done."))

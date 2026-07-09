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

# Only verified enrollments are reconciled (mirrors
# api.services.lifecycle._AUTH_ELIGIBLE_STAGES). pending_verification is
# deliberately excluded. A verified household stays at VERIFIED until its
# governing case authorization is approved, which advances it to
# kitchen_assignment; a later re-approval is picked up on the next run.
ELIGIBLE_STAGES = [
    EnrollmentStage.VERIFIED,
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
        # Materialize the eligible PKs up front so each enrollment is processed in
        # its OWN transaction below (a single bad row must not abort the batch).
        ids = list(
            EnrollmentVerification.objects.filter(stage__in=ELIGIBLE_STAGES)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if opts["limit"]:
            ids = ids[: opts["limit"]]

        transitions = Counter()
        scanned = 0
        changed = 0
        errors = 0

        # Dry-run rolls back each enrollment's own transaction so nothing
        # persists; --apply commits per enrollment.
        class _Rollback(Exception):
            pass

        for pk in ids:
            scanned += 1
            before = after = None
            try:
                # Per-enrollment atomic block: an IntegrityError (e.g. a case
                # already claimed by another live enrollment) rolls back ONLY this
                # enrollment and leaves the connection healthy for the next one.
                with transaction.atomic():
                    enr = (
                        EnrollmentVerification.objects.select_related("case").get(pk=pk)
                    )
                    before = enr.stage
                    reconcile_enrollment_authorization(enr)
                    after = (
                        EnrollmentVerification.objects.values_list("stage", flat=True)
                        .get(pk=pk)
                    )
                    if not apply:
                        raise _Rollback()
            except _Rollback:
                pass
            except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
                errors += 1
                self.stderr.write(f"  enrollment {pk}: {exc}")
                continue
            if before is not None and after is not None and after != before:
                changed += 1
                transitions[f"{before} -> {after}"] += 1

        verb = "changed" if apply else "would change"
        self.stdout.write(
            f"Scanned {scanned} eligible enrollments; {changed} {verb}; {errors} error(s)."
        )
        for transition, n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {transition}: {n}")
        if not apply:
            self.stdout.write(
                self.style.WARNING("DRY RUN - nothing persisted. Re-run with --apply to commit.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("Done."))

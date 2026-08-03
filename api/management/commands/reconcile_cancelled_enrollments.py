"""Reconcile CANCELLED enrollments so they can be reactivated where the data
supports it, driven by each enrollment's GOVERNING internal-service case.

Cancelled is a terminal stage with no reactivate action, so members cancelled in
error get stuck. This command re-derives the right resting stage from the
governing case + its authorization, one member at a time:

* Governing case OPEN + authorization APPROVED / NOT_REQUIRED  -> ON_HOLD
  (reversible: an agent can Resume, which rebuilds the delivery calendar).
* Governing case OPEN + authorization DENIED / NEVER_REQUESTED -> CLOSED
  (no service is authorized; a later re-approval / new case reopens it).
* Governing case CLOSED / CANCELLED, or NO internal-service case at all -> CLOSED
  (reversible closure off-ramp: a new open case reopens service).
* Governing case OPEN + authorization PENDING / blank -> ON_HOLD
  (awaiting authorization; reversible once approved).

Only STAGE transitions are made here (via advance_enrollment(force=True), which
logs a StageEvent + note). It does NOT rebuild calendars or reactivate member
profiles -- that happens when an agent Resumes an ON_HOLD member (which now
recomputes the plan from the governing authorization and rebuilds the calendar).

Dry-run by default; pass --apply to commit. Idempotent -- re-running only acts on
rows still at CANCELLED. Optional --days N and --client <id> narrow the scope.
"""
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    ServiceAuthorizationStatus,
)
from api.services.lifecycle import (
    _CLOSED_CASE_STATUSES,
    _DENIED_EQUIVALENT_STATUSES,
    advance_enrollment,
    governing_internal_case,
)

_APPROVED = {
    ServiceAuthorizationStatus.APPROVED,
    ServiceAuthorizationStatus.NOT_REQUIRED,
}
_ACTOR_LABEL = "system:cancelled-reconcile"


# Stages that OCCUPY a case for the uniq_enrollment_verification_per_case
# constraint (i.e. NOT excluded). Reviving a cancelled enrollment INTO one of
# these on a case that already has such an enrollment would collide.
_LIVE_STAGES = [
    EnrollmentStage.PENDING_VALIDATION,
    EnrollmentStage.VALIDATED,
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.ON_HOLD,
    EnrollmentStage.SERVICE_COMPLETE,
]


def _client_has_other_live_enrollment(enr):
    """True when the CLIENT already has ANOTHER non-excluded (live) enrollment.

    Reviving this cancelled row would create a second live enrollment for a
    member who is already being served (or held) -- a duplicate. This is broader
    than the per-case check: a cancelled row is frequently UNBOUND (case=None),
    so a case-only guard misses it and revives a duplicate next to the member's
    real active enrollment (the "on hold for no reason" bug)."""
    return (
        EnrollmentVerification.objects.filter(
            client_id=enr.client_id, stage__in=_LIVE_STAGES
        )
        .exclude(pk=enr.pk)
        .exists()
    )


def _decide(enr):
    """Return ``(target_stage, reason)`` for a cancelled enrollment based on its
    governing internal-service case + authorization."""
    # A revive would duplicate an enrollment the client already has live (bound
    # or unbound) -- close this cancelled row instead of reviving a duplicate.
    if _client_has_other_live_enrollment(enr):
        return EnrollmentStage.CLOSED, "duplicate: client already has a live enrollment"
    gov = governing_internal_case(enr)
    if gov is None:
        return EnrollmentStage.CLOSED, "no governing internal-service case"
    if gov.case_status in _CLOSED_CASE_STATUSES:
        return EnrollmentStage.CLOSED, "governing case closed/cancelled"
    auth = gov.service_authorization_status
    if auth in _DENIED_EQUIVALENT_STATUSES:
        return EnrollmentStage.CLOSED, "governing case open + authorization denied"
    if auth in _APPROVED:
        return EnrollmentStage.ON_HOLD, "governing case open + authorization approved"
    # Open but not yet approved (pending / blank): reversible wait.
    return EnrollmentStage.ON_HOLD, "governing case open + awaiting authorization"


class Command(BaseCommand):
    help = (
        "Move CANCELLED enrollments to On Hold (reactivatable) or Closed based on "
        "their governing internal-service case status + authorization."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Commit the changes (default: dry run, prints what WOULD change).",
        )
        parser.add_argument(
            "--days", type=int, default=None,
            help="Only enrollments cancelled within the last N days.",
        )
        parser.add_argument(
            "--client", type=str, default=None,
            help="Limit to a single client id.",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Process at most N enrollments (for a controlled first pass).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = EnrollmentVerification.objects.filter(
            stage=EnrollmentStage.CANCELLED
        ).select_related("client").order_by("-stage_at")
        if opts["days"]:
            qs = qs.filter(stage_at__gte=timezone.now() - timedelta(days=opts["days"]))
        if opts["client"]:
            qs = qs.filter(client_id=opts["client"])
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        total = qs.count() if not opts["limit"] else len(qs)
        self.stdout.write(f"Cancelled enrollments in scope: {total}")

        buckets = Counter()
        errors = 0
        for enr in qs:
            target, reason = _decide(enr)
            buckets[f"{target} :: {reason}"] += 1
            if not apply:
                continue
            note = f"Cancelled reconcile -> {target}: {reason}."
            # Own transaction per row so one failure rolls back only that row and
            # the run keeps going (a DB IntegrityError otherwise aborts the loop).
            try:
                with transaction.atomic():
                    advance_enrollment(
                        enr, target, force=True,
                        actor_label=_ACTOR_LABEL, note=note,
                        trigger="reconcile.cancelled_enrollment",
                    )
            except Exception as exc:  # noqa: BLE001 - isolate + report, never abort the run
                errors += 1
                buckets[f"{target} :: {reason}"] -= 1
                buckets[f"ERROR :: {type(exc).__name__}"] += 1
                self.stderr.write(f"  SKIP enr {enr.pk} (client {enr.client_id}): {exc}")

        self.stdout.write("")
        self.stdout.write("Planned outcomes:" if not apply else "Applied outcomes:")
        for key, n in sorted(buckets.items()):
            self.stdout.write(f"  {n:5d}  {key}")
        on_hold = sum(n for k, n in buckets.items() if k.startswith(str(EnrollmentStage.ON_HOLD)))
        closed = sum(n for k, n in buckets.items() if k.startswith(str(EnrollmentStage.CLOSED)))
        self.stdout.write("")
        self.stdout.write(f"On Hold (reactivatable): {on_hold} | Closed: {closed} | errors: {errors}")
        if not apply:
            self.stdout.write("")
            self.stdout.write("DRY RUN -- nothing changed. Re-run with --apply to commit.")

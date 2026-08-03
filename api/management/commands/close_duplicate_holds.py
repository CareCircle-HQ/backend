"""Close ON_HOLD enrollments that are duplicates of a member's live service.

The Aug-3 ``reconcile_cancelled_enrollments`` run revived some CANCELLED, often
UNBOUND (case=None) enrollments to On Hold even though the client already had a
``service_active`` enrollment -- leaving a member "On Hold for no reason" next to
their real active enrollment. This closes those duplicate On Hold rows.

A hold is a duplicate to close ONLY when the client has another SERVICE_ACTIVE
enrollment AND the held one is either unbound (no case) or the SAME product kind
as an active one -- so a legitimate different-product hold (e.g. meals active +
boxes on hold) is left untouched. Review-only by default; ``--apply`` commits.
Idempotent and per-row transactional.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.catalog import product_kind_for_enrollment
from api.services.lifecycle import advance_enrollment

_ACTOR_LABEL = "system:close-duplicate-holds"


class Command(BaseCommand):
    help = (
        "Close ON_HOLD enrollments that duplicate a member's live SERVICE_ACTIVE "
        "enrollment (revived case-less duplicates from the cancelled reconcile)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Commit (default: review/dry-run).")
        parser.add_argument("--client", type=str, default=None,
                            help="Limit to a single client id.")
        parser.add_argument("--limit", type=int, default=None)

    def _is_duplicate(self, enr):
        """(True, reason) when this on-hold enrollment duplicates a live active
        one for the same client; (False, why) otherwise."""
        actives = list(
            EnrollmentVerification.objects
            .filter(client_id=enr.client_id, stage=EnrollmentStage.SERVICE_ACTIVE)
            .exclude(pk=enr.pk)
        )
        if not actives:
            return False, "no active enrollment (legit hold)"
        if enr.case_id is None:
            return True, "unbound hold next to an active enrollment"
        kind = product_kind_for_enrollment(enr)
        for a in actives:
            if product_kind_for_enrollment(a) == kind:
                return True, "same-kind hold next to an active enrollment"
        return False, "different product kind (legit second-product hold)"

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = (
            EnrollmentVerification.objects
            .filter(stage=EnrollmentStage.ON_HOLD)
            .select_related("client", "case")
            .order_by("-stage_at")
        )
        if opts["client"]:
            qs = qs.filter(client_id=opts["client"])
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        rows = list(qs)
        self.stdout.write(f"On-hold enrollments in scope: {len(rows)}")
        buckets = Counter()
        closed = errors = 0
        for enr in rows:
            dup, reason = self._is_duplicate(enr)
            if not dup:
                buckets[f"skip :: {reason}"] += 1
                continue
            buckets[f"CLOSE :: {reason}"] += 1
            self.stdout.write(
                f"  CLOSE enr {enr.pk} client {enr.client_id} "
                f"case {str(enr.case_id)[:8] if enr.case_id else None} :: {reason}"
            )
            if not apply:
                continue
            try:
                with transaction.atomic():
                    advance_enrollment(
                        enr, EnrollmentStage.CLOSED, force=True,
                        actor_label=_ACTOR_LABEL,
                        note="Closed duplicate On Hold: the member already has a "
                             "live active enrollment.",
                        trigger="cleanup.duplicate_hold_closed",
                    )
                    enr.close_reason = "duplicate_of_active"
                    enr.save(update_fields=["close_reason"])
                closed += 1
            except Exception as exc:  # noqa: BLE001 - isolate + report
                errors += 1
                buckets[f"ERROR :: {type(exc).__name__}"] += 1
                self.stderr.write(f"    FAILED enr {enr.pk}: {exc}")

        self.stdout.write("")
        self.stdout.write("Summary:")
        for k, n in sorted(buckets.items()):
            self.stdout.write(f"  {n:5d}  {k}")
        self.stdout.write("")
        self.stdout.write(f"Closed: {closed} | errors: {errors} | applied={apply}")
        if not apply:
            self.stdout.write("REVIEW ONLY -- nothing changed. Re-run with --apply to commit.")

"""Close ON_HOLD enrollments whose service has actually ended.

An agent close-out (or an all-members-paused auto-hold) parks a household at
``on_hold`` -- correctly off every Purchase Order -- pending the related
internal-service case being closed in Unite Us. When that case IS closed, the
enrollment should transition to ``closed``. But nothing finishes that step, so
the row lingers at ``on_hold`` with a CLOSED governing case:

  * it contradicts the closed case (the UI reads the case/lifecycle as closed
    while the enrollment stage stays on_hold),
  * it keeps the household in the Distribution "taken out of PO" list under the
    on_hold tab (it should read as closed / off-boarded), and
  * it leaves a stale NON-terminal enrollment that duplicate/supersession logic
    can trip over.

This closes exactly those: an ``on_hold`` enrollment whose GOVERNING internal-
service case is closed/cancelled or absent. An on_hold household that STILL has
an open governing internal-service case (a genuine hold pending resume) is left
untouched. Mirrors ``reconcile_cancelled_enrollments`` (same governing-case
decision + ``advance_enrollment(force=True)`` audit), for the on_hold source.

Dry-run by default. Idempotent.

Usage:
    python manage.py reconcile_onhold_closed_cases            # dry run
    python manage.py reconcile_onhold_closed_cases --apply
    python manage.py reconcile_onhold_closed_cases --apply --limit 50
    python manage.py reconcile_onhold_closed_cases --client <id>
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    CaseType,
    EnrollmentStage,
    EnrollmentVerification,
)
from api.services.lifecycle import (
    _CLOSED_CASE_STATUSES,
    advance_enrollment,
    governing_internal_case,
)

_ACTOR_LABEL = "system:onhold-closed-case-reconcile"


def _client_has_open_isc(client_id):
    """True when the CLIENT still has ANY open (not closed/cancelled) internal-
    service case -- i.e. an active meal/box program somewhere. Broader than the
    enrollment's governing case: guards against closing a hold when the client
    is still served under a different open case (``governing_internal_case`` can
    resolve to None/closed even when a separate open ISC exists)."""
    from api.models import Case
    return (
        Case.objects.filter(client_id=client_id, case_type=CaseType.INTERNAL_SERVICE)
        .exclude(case_status__in=_CLOSED_CASE_STATUSES)
        .exists()
    )


def _should_close(enr):
    """An on_hold enrollment should close when its service has genuinely ended:
    the GOVERNING internal-service case is closed/cancelled or absent, AND the
    client has NO other open internal-service case. Returns (bool, reason)."""
    gov = governing_internal_case(enr)
    gov_ended = gov is None or gov.case_status in _CLOSED_CASE_STATUSES
    if not gov_ended:
        return False, "governing case still open -- left on hold"
    # Belt-and-suspenders: never close a hold while the client still has an open
    # internal-service case (they may still be served under a different program).
    if _client_has_open_isc(enr.client_id):
        return False, "client has another open internal-service case -- left on hold"
    return True, ("no governing internal-service case" if gov is None
                  else "governing internal-service case closed/cancelled")


class Command(BaseCommand):
    help = (
        "Close ON_HOLD enrollments whose governing internal-service case is "
        "closed/cancelled/absent (service ended), leaving genuine open-case holds "
        "untouched. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist changes.")
        parser.add_argument("--client", type=str, default=None, help="Limit to one client id.")
        parser.add_argument("--limit", type=int, default=0, help="Cap enrollments processed.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = (
            EnrollmentVerification.objects
            .filter(stage=EnrollmentStage.ON_HOLD)
            .select_related("client")
            .order_by("pk")
        )
        if opts["client"]:
            qs = qs.filter(client_id=opts["client"])

        to_close, left = [], Counter()
        for enr in qs.iterator():
            close, reason = _should_close(enr)
            if close:
                to_close.append((enr, reason))
            else:
                left[reason] += 1

        if opts["limit"]:
            to_close = to_close[: opts["limit"]]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Close on-hold enrollments whose case has ended ==="
        ))
        self.stdout.write(f"  on_hold -> CLOSED: {len(to_close)}"
                          + (f"  (limited to {opts['limit']})" if opts["limit"] else ""))
        by_reason = Counter(r for _, r in to_close)
        for r, n in by_reason.most_common():
            self.stdout.write(f"     {n:5}  {r}")
        for enr, _r in to_close[:20]:
            c = enr.client
            name = (f"{c.first_name} {c.last_name}".strip() if c else "") or "?"
            self.stdout.write(f"    close enr={enr.pk} {name[:26]:26} client={enr.client_id}")
        if len(to_close) > 20:
            self.stdout.write(f"    ... and {len(to_close) - 20} more")
        if left:
            self.stdout.write("  LEFT on hold (open governing case): "
                              + ", ".join(f"{n} {r}" for r, n in left.most_common()))

        if not apply:
            self.stdout.write(self.style.WARNING("\nDry run -- re-run with --apply."))
            return

        closed = errors = 0
        for enr, reason in to_close:
            note = f"On-hold reconcile -> Closed: {reason}."
            try:
                with transaction.atomic():
                    advance_enrollment(
                        enr, EnrollmentStage.CLOSED, force=True,
                        actor_label=_ACTOR_LABEL, note=note,
                        trigger="reconcile.onhold_closed_case",
                    )
                    try:
                        from api.services.orders import truncate_future_deliveries
                        truncate_future_deliveries(enr)
                    except Exception:  # noqa: BLE001 - never abort the close on cleanup
                        pass
                closed += 1
            except Exception as exc:  # noqa: BLE001 - isolate + report, keep going
                errors += 1
                self.stderr.write(f"  SKIP enr {enr.pk} (client {enr.client_id}): {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: closed {closed} on-hold enrollment(s); errors: {errors}."
        ))

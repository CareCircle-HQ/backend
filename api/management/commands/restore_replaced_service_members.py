"""Repair members demoted by a governing-case REPLACEMENT.

When the governing internal-service case id changed, the replacement closed the
member's live enrollment and opened a NEW one that -- when the kitchen/cadence
did not carry -- landed in Kitchen Assignment / Pending Verification / On Hold
instead of Service Active. Those members fell off the Purchase Order even though
their governing case is open + approved.

This command restores them: for each LIVE enrollment that SUPERSEDES a closed
``case_replaced`` enrollment and is NOT Service Active, it carries the previous
kitchen + cadence (from the superseded enrollment, or its own if already set),
returns any excluded member to service, advances the enrollment to Service
Active, (re)creates the delivery plan and REBUILDS the calendar from today -- so
they rejoin future Purchase Orders.

Only acts when the governing internal-service case is OPEN + APPROVED/NOT-REQUIRED
(service is actually authorized). Review-only by default; pass --apply to commit.
Idempotent: an already-active enrollment is skipped, so re-running is safe.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    MemberStatus,
    ServiceAuthorizationStatus,
)
from api.services.catalog import product_kind_for_enrollment
from api.services.delivery import (
    create_member_delivery_schedules,
    current_household_cadence,
)
from api.services.lifecycle import (
    _CLOSED_CASE_STATUSES,
    advance_enrollment,
    governing_internal_case,
)
from api.services.meal_rules import reconcile_member_kitchen_output
from api.services.orders import rebuild_delivery_calendar

_ACTOR_LABEL = "system:restore-replaced-service"
_FAVORABLE = {
    ServiceAuthorizationStatus.APPROVED,
    ServiceAuthorizationStatus.NOT_REQUIRED,
}
# Live (non-terminal) stages that are NOT yet serving -- the ones we restore.
_RESTORABLE_STAGES = {
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.ON_HOLD,
}
_ONCE_A_WEEK = "once_a_week"


class Command(BaseCommand):
    help = (
        "Restore members demoted by a governing-case replacement: carry the "
        "previous kitchen/cadence, set Service Active, and rebuild the calendar "
        "from today so they rejoin future POs."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Commit changes (default: review/dry-run).")
        parser.add_argument("--client", type=str, default=None,
                            help="Limit to a single client id.")
        parser.add_argument("--limit", type=int, default=None,
                            help="Process at most N enrollments.")

    def _candidates(self, opts):
        qs = (
            EnrollmentVerification.objects.filter(
                stage__in=[s.value for s in _RESTORABLE_STAGES],
                supersedes__isnull=False,
            )
            .select_related("client", "kitchen", "supersedes", "supersedes__kitchen", "case")
            .order_by("-opened_at")
        )
        if opts["client"]:
            qs = qs.filter(client_id=opts["client"])
        if opts["limit"]:
            qs = qs[: opts["limit"]]
        return qs

    def _carry_source(self, enr, want_kind):
        """Walk the supersedes chain (nearest first, incl. ``enr``) and return
        ``(kitchen, cadence, weekdays)`` from the last real serving state whose
        product kind MATCHES ``want_kind``.

        A member may have been replaced MORE THAN ONCE (e.g. the first bad import
        demoted them, then a re-import replaced the demoted -- kitchen-less --
        enrollment again), so we take the NEAREST kitchen and NEAREST cadence
        found anywhere in the chain. But we ONLY carry from a node of the SAME
        product kind: for a genuine meals<->boxes switch the old kitchen/cadence
        is for the wrong product and must not be reused (that member falls to
        "needs manual" instead of being wrongly activated on the old kitchen).
        """
        kitchen = cadence = weekdays = None
        seen, node = set(), enr
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            # Skip nodes whose product kind differs from the current enrollment.
            if want_kind is None or product_kind_for_enrollment(node) == want_kind:
                if kitchen is None and node.kitchen_id:
                    kitchen = node.kitchen
                if not cadence:
                    c = current_household_cadence(node)
                    if c:
                        cadence, weekdays = c, node.delivery_weekdays
            if kitchen is not None and cadence:
                break
            node = node.supersedes
        return kitchen, cadence, weekdays

    def _plan(self, enr):
        """Return (ok, kitchen, cadence, weekdays, kind, reason)."""
        old = enr.supersedes
        if old is None or (old.close_reason or "") != "case_replaced":
            return False, None, None, None, None, "supersedes is not a case_replaced enrollment"
        gov = governing_internal_case(enr)
        if gov is None:
            return False, None, None, None, None, "no governing internal-service case"
        if gov.case_status in _CLOSED_CASE_STATUSES:
            return False, None, None, None, None, "governing case closed/cancelled"
        if gov.service_authorization_status not in _FAVORABLE:
            return False, None, None, None, None, "governing case not approved"
        kind = product_kind_for_enrollment(enr)
        kitchen, cadence, weekdays = self._carry_source(enr, kind)
        if kitchen is None:
            return False, None, None, None, kind, "no same-kind kitchen to carry (needs manual assignment)"
        if not cadence:
            return False, kitchen, None, None, kind, "no same-kind cadence to carry (needs manual assignment)"
        return True, kitchen, cadence, weekdays, kind, "restore -> service_active + rebuild calendar"

    @transaction.atomic
    def _apply_one(self, enr, kitchen, cadence, weekdays, kind):
        # Carry kitchen + weekdays onto the live enrollment.
        fields = []
        if enr.kitchen_id != kitchen.pk:
            enr.kitchen = kitchen
            fields.append("kitchen")
        if not enr.delivery_weekdays and weekdays:
            enr.delivery_weekdays = weekdays
            fields.append("delivery_weekdays")
        if fields:
            enr.save(update_fields=fields)
        # Return cancel/hold-excluded members to service (respect eligibility pause).
        for mv in enr.member_profiles.all():
            if getattr(mv, "eligibility_paused", False):
                continue
            if mv.status == MemberStatus.INACTIVE:
                reconcile_member_kitchen_output(
                    mv, kitchen=kitchen, allow_resume=True, save=True,
                )
        # Advance to Service Active (force: verification already happened before
        # the replacement; restoring the prior service state must not be re-gated).
        if EnrollmentStage(enr.stage) != EnrollmentStage.SERVICE_ACTIVE:
            advance_enrollment(
                enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
                actor_label=_ACTOR_LABEL,
                note="Restored service after governing-case replacement: carried "
                     "the previous kitchen + cadence.",
            )
        # (Re)create the delivery plan from the carried cadence, then rebuild the
        # dated calendar from today so they rejoin future Purchase Orders.
        once_weekday = None
        if cadence == _ONCE_A_WEEK:
            wds = enr.delivery_weekdays or []
            once_weekday = wds[0] if wds else None
        create_member_delivery_schedules(
            enr, case=enr.case, cadence=cadence, once_a_week_weekday=once_weekday,
            kitchen=kitchen, product_kind=kind,
        )
        rebuild_delivery_calendar(enr, from_date=timezone.localdate())

    def handle(self, *args, **opts):
        apply = opts["apply"]
        buckets = Counter()
        restored = errors = 0
        rows = list(self._candidates(opts))
        self.stdout.write(f"Superseded live enrollments in scope: {len(rows)}")
        for enr in rows:
            ok, kitchen, cadence, weekdays, kind, reason = self._plan(enr)
            tag = ("RESTORE" if ok else "SKIP") + " :: " + reason
            buckets[tag] += 1
            kn = kitchen.name if kitchen else None
            self.stdout.write(
                f"  {'RESTORE' if ok else 'skip   '} enr {enr.pk} client {enr.client_id} "
                f"stage={enr.stage} kitchen={kn} cadence={cadence or '-'} kind={kind or '-'} :: {reason}"
            )
            if ok and apply:
                try:
                    self._apply_one(enr, kitchen, cadence, weekdays, kind)
                    restored += 1
                except Exception as exc:  # noqa: BLE001 - isolate, report, continue
                    errors += 1
                    buckets["ERROR :: " + type(exc).__name__] += 1
                    self.stderr.write(f"    FAILED enr {enr.pk}: {exc}")

        self.stdout.write("")
        self.stdout.write("Summary:")
        for k, n in sorted(buckets.items()):
            self.stdout.write(f"  {n:5d}  {k}")
        self.stdout.write("")
        self.stdout.write(
            f"Restored: {restored} | errors: {errors} | applied={apply}"
        )
        if not apply:
            self.stdout.write("REVIEW ONLY -- nothing changed. Re-run with --apply to commit.")

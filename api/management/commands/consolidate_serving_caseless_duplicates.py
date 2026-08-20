"""Consolidate DUPLICATE SERVING enrollments: a client has a CASELESS serving
(service_active/on_hold) enrollment AND a separate live, NON-pending sibling that
holds the governing internal-service case (the two-serving-row duplicates the
plain consolidate command skips).

Calendar-aware: the survivor is the enrollment that actually owns the active
delivery calendar (future OrderSchedule occurrences), so we never drop live
deliveries:

  * keeper (caseless) has deliveries, holder none  -> move the case onto the
    keeper (disregard the holder); the deliveries were running WITHOUT a case.
  * holder has deliveries, keeper none             -> disregard the caseless dup.
  * neither has deliveries (both idle/on_hold)     -> keep the case-holder,
    disregard the caseless dup.
  * BOTH have live deliveries                       -> SKIP (manual review): can't
    merge two live calendars blindly.

Atomic per client (a failure rolls that client back and the run continues) and
global-constraint-safe. DRY-RUN by default; --apply to write; --client to scope.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    OrderSchedule,
)
from api.services.lifecycle import (
    governing_internal_case,
    reconcile_internal_service_authorization,
)

_SERVING = {EnrollmentStage.SERVICE_ACTIVE.value, EnrollmentStage.ON_HOLD.value}
_TERMINAL = {
    EnrollmentStage.CLOSED.value,
    EnrollmentStage.CANCELLED.value,
    EnrollmentStage.DISREGARDED.value,
}
_PENDING = EnrollmentStage.PENDING_VERIFICATION.value
_ALLOWED_HOLDER_STAGES = _SERVING | {
    EnrollmentStage.KITCHEN_ASSIGNMENT.value,
    EnrollmentStage.VERIFIED.value,
    EnrollmentStage.VALIDATED.value,
}


class Command(BaseCommand):
    help = (
        "Consolidate duplicate serving enrollments (caseless serving + a live "
        "sibling holding the case), keeping whichever owns the active delivery "
        "calendar. dry-run; --apply; --client."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--client", default="")

    def _future_occ(self, enr):
        return OrderSchedule.objects.filter(
            enrollment=enr, anticipated_delivery_date__gte=timezone.localdate(),
        ).count()

    def _disregard(self, enr):
        enr.case = None
        enr.stage = EnrollmentStage.DISREGARDED.value
        enr.close_reason = "serving_duplicate_fix"
        enr.save(update_fields=["case", "stage", "close_reason"])

    def handle(self, *args, **opts):
        apply = opts["apply"]
        only = (opts.get("client") or "").strip()

        caseless = EnrollmentVerification.objects.filter(
            case__isnull=True, stage__in=_SERVING,
        ).select_related("client")
        if only:
            caseless = caseless.filter(client__client_id=only)

        moved = disregarded_dup = skipped = both = 0
        for keeper in caseless:
            gov = governing_internal_case(keeper)
            if gov is None or getattr(gov, "case_status", "") in {"closed", "cancelled"}:
                continue
            holders = [
                h for h in EnrollmentVerification.objects.filter(case_id=gov.case_id)
                .exclude(pk=keeper.pk)
                if h.stage not in _TERMINAL and h.stage != _PENDING
            ]
            if len(holders) != 1:
                # 0 -> handled by the plain binder; 2+ -> ambiguous, manual.
                if len(holders) > 1:
                    skipped += 1
                    self._print(keeper, gov, holders, "SKIP: multiple live holders (manual)")
                continue
            holder = holders[0]
            if holder.stage not in _ALLOWED_HOLDER_STAGES:
                # holder is some other live stage -> be safe, skip.
                skipped += 1
                self._print(keeper, gov, [holder], "SKIP: unexpected holder stage (manual)")
                continue

            k_occ = self._future_occ(keeper)
            h_occ = self._future_occ(holder)

            if k_occ and h_occ:
                both += 1
                self._print(keeper, gov, [holder],
                            f"SKIP: BOTH have live calendars (keeper {k_occ}, holder {h_occ}) (manual)")
                continue

            if k_occ and not h_occ:
                action = "MOVE case -> keeper (keeper owns calendar), disregard holder"
                survivor, drop, bind = keeper, holder, True
            else:
                # holder owns the calendar, or neither does -> keep the case-holder.
                action = ("keep holder (owns calendar), disregard caseless dup"
                          if h_occ else "both idle -> keep case-holder, disregard caseless dup")
                survivor, drop, bind = holder, keeper, False

            self._print(keeper, gov, [holder],
                        f"FIX{' [SERVING]' if survivor.stage in _SERVING else ''}: {action}")
            if apply:
                try:
                    with transaction.atomic():
                        self._disregard(drop)
                        if bind:
                            survivor.case = gov
                            survivor.save(update_fields=["case"])
                        reconcile_internal_service_authorization(survivor.client)
                except Exception as exc:  # noqa: BLE001 - report, roll back, continue
                    self.stdout.write(self.style.ERROR(f"      FAILED, skipped (rolled back): {exc}"))
                    skipped += 1
                    continue
            if bind:
                moved += 1
            else:
                disregarded_dup += 1

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}: {moved} case moved to the delivering enrollment, "
            f"{disregarded_dup} caseless duplicate(s) disregarded, "
            f"{both} both-live-calendar skipped, {skipped} other skipped."
        ))

    def _print(self, keeper, gov, holders, note):
        self.stdout.write(
            f"  {keeper.client.client_id} {keeper.client.first_name} {keeper.client.last_name} | {note}\n"
            f"      caseless keeper enr {keeper.pk} ({keeper.stage}) | gov case {str(gov.case_id)[:8]} "
            f"({(gov.program_name or gov.service_type or '')[:30]})\n"
            f"      holder(s): {[(h.pk, h.stage) for h in holders]}"
        )

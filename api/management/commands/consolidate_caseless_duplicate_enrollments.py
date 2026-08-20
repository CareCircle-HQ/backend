"""Consolidate duplicate enrollments where a client's REAL (most-advanced) live
enrollment is CASELESS while a stray ``pending_verification`` sibling holds the
governing internal-service case.

Background: before the fix, verifying a client who already had a
``pending_verification`` enrollment (created by the import, holding the case)
produced a SECOND live enrollment that couldn't take the case (per-case unique
constraint) and finished caseless -- two live enrollments for one case, with the
real one left caseless (the TEMEKA KING / 6b8f5acd pattern). The verification
wizard now reuses the pending row, but existing rows need repair.

For each affected client this binds the governing case onto the REAL (most-
advanced) enrollment and DISREGARDS the stray ``pending_verification`` holder(s),
then re-reconciles. SAFE-ONLY: a client is SKIPPED (reported for manual review)
when a NON-pending sibling holds the case, so a genuine parallel/serving program
is never disturbed.

DRY-RUN by default; ``--apply`` to write. ``--client`` scopes to one client_id.
"""

from django.core.management.base import BaseCommand

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import (
    governing_internal_case,
    reconcile_internal_service_authorization,
)

_TERMINAL = {
    EnrollmentStage.CLOSED.value,
    EnrollmentStage.CANCELLED.value,
    EnrollmentStage.DISREGARDED.value,
}
_SERVING = {EnrollmentStage.SERVICE_ACTIVE.value, EnrollmentStage.ON_HOLD.value}
_RANK = {
    EnrollmentStage.PENDING_VALIDATION.value: 0,
    EnrollmentStage.SCHEDULED_EXTENSION.value: 0,
    EnrollmentStage.VALIDATED.value: 1,
    EnrollmentStage.PENDING_VERIFICATION.value: 1,
    EnrollmentStage.VERIFIED.value: 2,
    EnrollmentStage.KITCHEN_ASSIGNMENT.value: 3,
    EnrollmentStage.SERVICE_ACTIVE.value: 4,
    EnrollmentStage.ON_HOLD.value: 4,
    EnrollmentStage.SERVICE_COMPLETE.value: 5,
}


class Command(BaseCommand):
    help = (
        "Consolidate caseless real enrollment + pending stray holding the case "
        "(dry-run; --apply to fix; --client to scope)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--client", default="")

    def _rank(self, e):
        return (_RANK.get(e.stage, 0), e.opened_at or e.pk)

    def handle(self, *args, **opts):
        apply = opts["apply"]
        only = (opts.get("client") or "").strip()

        caseless = EnrollmentVerification.objects.filter(
            case__isnull=True,
        ).exclude(stage__in=_TERMINAL)
        if only:
            caseless = caseless.filter(client__client_id=only)
        client_ids = list(dict.fromkeys(caseless.values_list("client_id", flat=True)))

        _PENDING = EnrollmentStage.PENDING_VERIFICATION.value
        fixed = skipped = serving_fixed = 0
        for cid in client_ids:
            live = list(
                EnrollmentVerification.objects.filter(client_id=cid)
                .exclude(stage__in=_TERMINAL)
                .select_related("client")
            )
            keeper = max(live, key=self._rank)
            gov = governing_internal_case(keeper)
            if gov is None or getattr(gov, "case_status", "") in {"closed", "cancelled"}:
                continue
            gov_id = str(gov.case_id)
            siblings = [e for e in live if e.pk != keeper.pk]

            # A NON-pending sibling that holds the governing case (or is itself
            # caseless) is a real enrollment -> never auto-touch; flag for manual
            # review so a genuine parallel/serving program is never disturbed.
            risky = [
                s for s in siblings
                if s.stage != _PENDING and (str(s.case_id) == gov_id or s.case_id is None)
            ]
            if risky:
                skipped += 1
                self._print(keeper, gov, risky, note="SKIP: non-pending sibling involved (manual)",
                            label="involved")
                continue

            # Pending-verification strays to disregard: caseless ones, or ones
            # holding the governing case (import/verify leftovers). A pending row
            # holding a DIFFERENT case is a separate program -> left alone.
            strays = [
                s for s in siblings
                if s.stage == _PENDING and (s.case_id is None or str(s.case_id) == gov_id)
            ]
            need_bind = keeper.case_id is None
            if not strays and not need_bind:
                continue  # nothing to consolidate

            serving = keeper.stage in _SERVING
            self._print(keeper, gov, strays,
                        note=("FIX" + (" [SERVING]" if serving else "")
                              + ("" if not need_bind else " (bind case to keeper)")))
            if apply:
                for h in strays:
                    h.case = None
                    h.stage = EnrollmentStage.DISREGARDED.value
                    h.close_reason = "caseless_duplicate_fix"
                    h.save(update_fields=["case", "stage", "close_reason"])
                if need_bind:
                    keeper.case = gov  # gov is now free (strays holding it were unbound)
                    keeper.save(update_fields=["case"])
                try:
                    reconcile_internal_service_authorization(keeper.client)
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.ERROR(f"      reconcile FAILED: {exc}"))
            fixed += 1
            if serving:
                serving_fixed += 1

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}: {fixed} client(s) consolidated "
            f"({serving_fixed} actively serving), {skipped} skipped for manual review."
        ))

    def _print(self, keeper, gov, holders, *, note, label="disregard"):
        self.stdout.write(
            f"  {keeper.client.client_id} {keeper.client.first_name} {keeper.client.last_name} | {note}\n"
            f"      keeper enr {keeper.pk} ({keeper.stage}) <- case {str(gov.case_id)[:8]} "
            f"({(gov.program_name or gov.service_type or '')[:34]})\n"
            f"      {label}: {[(h.pk, h.stage) for h in holders]}"
        )

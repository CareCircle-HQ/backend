"""Re-reconcile clients whose GOVERNING internal-service pointer was stamped to a
DEFERRED, future-dated case.

Background: before the deferral fix, a future-dated approved case (a
reauthorization or a different-kind Meals<->Boxes switch whose authorization
window starts in the FUTURE) could be selected as the governing case immediately,
supplanting the currently-serving case (the LUIS RAMOS prod bug). The serving
enrollment's own ``case`` FK was NOT changed, so delivery kept working, but
``Client.governing_internal_case_id`` (and downstream governing reads) pointed at
the wrong, future case.

This command finds clients whose stored ``governing_internal_case_id`` is a case
that ``deferred_extension_case_ids`` now defers, and re-runs
``reconcile_internal_service_authorization`` -- which (with the fixed
``pick_governing_case``) re-stamps governing back to the currently-serving case
and PARKS the future case as a non-serving ``SCHEDULED_EXTENSION`` that activates
when its window opens. No service interruption.

DRY-RUN by default (reports what WOULD change); pass ``--apply`` to run the
reconcile. ``--client`` scopes to one client_id.
"""

from django.core.management.base import BaseCommand

from api.models import Case, CaseType, Client, EnrollmentStage
from api.services.lifecycle import (
    deferred_extension_case_ids,
    pick_governing_case,
    reconcile_internal_service_authorization,
)

_SERVING = {EnrollmentStage.SERVICE_ACTIVE.value, EnrollmentStage.ON_HOLD.value}


class Command(BaseCommand):
    help = (
        "Re-reconcile clients whose governing case is a deferred future case "
        "(dry-run; --apply to fix; --client to scope)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Run the reconcile (default is a dry-run).")
        parser.add_argument("--client", default="",
                            help="Only this client_id (default: all affected).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        only = (opts.get("client") or "").strip()

        client_ids = set(
            Case.objects.filter(
                case_type=CaseType.INTERNAL_SERVICE, is_extension=True,
            ).values_list("client_id", flat=True)
        )
        if only:
            client_ids = {cid for cid in client_ids if str(cid) == only}

        rows = []
        for cid in client_ids:
            client = Client.objects.filter(pk=cid).first()
            if client is None:
                continue
            cases = [c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE]
            deferred = {str(x) for x in deferred_extension_case_ids(cases)}
            if not deferred:
                continue
            stamp = str(client.governing_internal_case_id) if client.governing_internal_case_id else ""
            if not stamp or stamp not in deferred:
                continue
            corrected = pick_governing_case(cases)
            serving = client.enrollments.filter(stage__in=_SERVING).exists()
            rows.append((client, stamp, corrected, serving))

        self.stdout.write(f"Affected clients: {len(rows)}\n")
        applied = 0
        for client, stamp, corrected, serving in rows:
            flag = " [SERVING]" if serving else ""
            self.stdout.write(
                f"  {client.client_id} {client.first_name} {client.last_name}{flag}\n"
                f"      governing stamped -> {stamp[:8]} (future/deferred)\n"
                f"      corrected -> {str(corrected.case_id)[:8]} "
                f"({(corrected.program_name or corrected.service_type or '')[:40]})"
            )
            if apply:
                try:
                    reconcile_internal_service_authorization(client)
                    applied += 1
                except Exception as exc:  # noqa: BLE001 - report, keep going
                    self.stdout.write(self.style.ERROR(f"      FAILED: {exc}"))

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        n = applied if apply else len(rows)
        self.stdout.write(self.style.SUCCESS(f"\n{mode}: {n} client(s) reconciled."))

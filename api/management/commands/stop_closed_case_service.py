"""Restore the "last internal-service case closed -> service stops" invariant
for clients where it was never applied (historical closures that bypassed the
inline reconcile), so they stop landing on Purchase Orders / the delivery
calendar.

A client is a candidate when:
  * they have at least one internal-service case,
  * NONE of those cases is still open (all closed/cancelled), and
  * they still have a non-terminal enrollment (not Closed/Cancelled).

For each candidate it re-runs :func:`reconcile_internal_service_authorization`
-- the exact, idempotent logic case closure uses -- which truncates future
deliveries and cancels the enrollment(s).

DRY-RUN BY DEFAULT: prints candidates and makes NO changes. Pass ``--apply`` to
actually run the close-out.

Usage:
    python manage.py stop_closed_case_service --file ids.txt          # dry-run
    python manage.py stop_closed_case_service --all                   # dry-run, whole DB
    python manage.py stop_closed_case_service --file ids.txt --apply  # mutate
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import (
    Case,
    CaseType,
    Client,
    EnrollmentStage,
    OrderSchedule,
    OrderStatus,
)
from api.services.lifecycle import (
    open_internal_service_cases,
    reconcile_internal_service_authorization,
)

_TERMINAL = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED}


class Command(BaseCommand):
    help = "Cancel service for clients whose last internal-service case is closed (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("client_ids", nargs="*", help="One or more client UUIDs.")
        parser.add_argument("--file", dest="file", help="File with one client UUID per line.")
        parser.add_argument("--all", action="store_true", help="Scan every client with an internal-service case.")
        parser.add_argument("--apply", action="store_true", help="Actually run the close-out (mutates data).")

    def _load_ids(self, opts):
        if opts.get("all"):
            return list(
                Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
                .values_list("client_id", flat=True)
                .distinct()
            )
        ids = list(opts.get("client_ids") or [])
        if opts.get("file"):
            with open(opts["file"]) as fh:
                ids += [line.strip() for line in fh if line.strip()]
        seen, out = set(), []
        for cid in ids:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    def handle(self, *args, **opts):
        apply = opts.get("apply", False)
        today = timezone.localdate()
        ids = self._load_ids(opts)
        if not ids:
            self.stderr.write("No client ids. Pass ids, --file, or --all.")
            return

        head = self.style.MIGRATE_HEADING
        mode = self.style.ERROR("APPLY (mutating)") if apply else self.style.SUCCESS("DRY-RUN")
        self.stdout.write(head(f"stop_closed_case_service: {mode}, {len(ids)} client(s) to check\n"))

        candidates = []
        for cid in ids:
            client = (
                Client.objects.filter(pk=cid)
                .prefetch_related("cases", "enrollments")
                .first()
            )
            if client is None:
                continue
            isc = [c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE]
            if not isc:
                continue
            if open_internal_service_cases(client):
                continue  # still has an open internal-service case -> not a full stop
            non_terminal = [
                e for e in client.enrollments.all()
                if EnrollmentStage(e.stage) not in _TERMINAL
            ]
            if not non_terminal:
                continue
            candidates.append((client, non_terminal))

        self.stdout.write(f"Candidates: {len(candidates)}\n")
        applied = 0
        for client, enrs in candidates:
            future = OrderSchedule.objects.filter(
                member__client_id=client.pk,
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
            ).count()
            stages = sorted({str(e.stage) for e in enrs})
            self.stdout.write(
                f"  {client.pk} | {client.first_name} {client.last_name} "
                f"| stages={stages} | future_scheduled={future}"
            )
            if apply:
                result = reconcile_internal_service_authorization(
                    client, actor_label="Data fix: closed-case service stop"
                )
                applied += 1
                self.stdout.write(self.style.WARNING(f"      -> reconcile: {result}"))

        self.stdout.write(head(f"\nDone. Candidates={len(candidates)}, applied={applied}."))
        if not apply and candidates:
            self.stdout.write("Re-run with --apply to perform the close-out.")

"""Pull households with NO open+approved internal-service authorization off
Purchase Orders they were committed to before the authorization guardrail
existed.

Only an OPEN case with an APPROVED / Not Required authorization clears a
household for delivery (the same rule the PO generation guardrail enforces via
``authorized_internal_service_case_exists``). Two historical leaks put
unauthorized households on already-cut POs, and no existing tool removes them
because ``truncate_future_deliveries`` deliberately PRESERVES occurrences already
committed to a DeliveryOrder:

  * only an OPEN *pending* (Requested) case -- an initial request not yet granted;
  * service running off a CLOSED *approved* case while the sole open case is
    pending (``governing_case_key`` ranked the closed approval over the open
    pending one).

For each candidate this command:
  1. CANCELS every FUTURE (expected_delivery_date >= today), not-yet-delivered
     DeliveryOrder for the WHOLE household (status pending / ready_for_delivery /
     on_hold), on POs in ANY status -- pulling them off upcoming POs. Delivered /
     out-for-delivery / already-cancelled / failed / returned orders are left
     untouched (historical record / in-flight).
  2. Re-runs :func:`reconcile_internal_service_authorization` -- the idempotent
     lifecycle logic -- which truncates the future delivery calendar and pulls
     enrollment stages back to Waiting Authorization.

Reversible by design: once the pending case is approved, the nightly reconcile /
"Prepare Members for PO" re-advances the household and regenerates the calendar.

DRY-RUN BY DEFAULT: prints candidates + the exact orders it WOULD cancel and
makes NO changes. Pass ``--apply`` to mutate.

Usage:
    python manage.py pull_unauthorized_from_po_20260723 <client_id> ...      # dry-run
    python manage.py pull_unauthorized_from_po_20260723 --file ids.txt        # dry-run
    python manage.py pull_unauthorized_from_po_20260723 --all                 # dry-run, whole DB
    python manage.py pull_unauthorized_from_po_20260723 --file ids.txt --apply # mutate
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    Case,
    CaseType,
    Client,
    DeliveryOrder,
    DeliveryOrderStatus,
    HouseholdMember,
    OrderSchedule,
    OrderStatus,
    ServiceAuthorizationStatus,
)
from api.services.lifecycle import (
    _CLOSED_CASE_STATUSES,
    reconcile_internal_service_authorization,
)

# Authorization that actually clears a household for delivery.
_APPROVED = {
    ServiceAuthorizationStatus.APPROVED,
    ServiceAuthorizationStatus.NOT_REQUIRED,
}

# DeliveryOrder statuses we CANCEL: future orders not yet delivered or already
# in transit. Delivered / out-for-delivery are honored; cancelled / failed /
# returned are already terminal.
_CANCELLABLE_DO_STATUSES = {
    DeliveryOrderStatus.PENDING,
    DeliveryOrderStatus.READY_FOR_DELIVERY,
    DeliveryOrderStatus.ON_HOLD,
}


def _has_open_approved(client):
    """True when the client holds an OPEN internal-service case whose
    authorization is APPROVED / Not Required -- i.e. authorized for delivery."""
    return any(
        c.case_type == CaseType.INTERNAL_SERVICE
        and c.service_authorization_status in _APPROVED
        and c.case_status not in _CLOSED_CASE_STATUSES
        for c in client.cases.all()
    )


def _household_client_ids(client):
    """Every client id in the household the case-holder governs (the client plus
    all their household roster members, across any enrollment household)."""
    ids = {client.pk}
    membership = getattr(client, "household_membership", None)
    if membership is not None:
        for m in membership.household.members.all():
            if m.client_id:
                ids.add(m.client_id)
    for enr in client.enrollments.all():
        hh = getattr(enr, "household", None)
        if hh is not None:
            for m in hh.members.all():
                if m.client_id:
                    ids.add(m.client_id)
    return ids


class Command(BaseCommand):
    help = (
        "Cancel future undelivered DeliveryOrders for households with no "
        "open+approved internal-service authorization (dry-run by default)."
    )

    def add_arguments(self, parser):
        parser.add_argument("client_ids", nargs="*", help="One or more client UUIDs.")
        parser.add_argument("--file", dest="file", help="File with one client UUID per line.")
        parser.add_argument(
            "--all", action="store_true",
            help="Scan every client that holds an internal-service case.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually cancel orders + reconcile (mutates data).",
        )

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
            if cid and cid not in seen:
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
        self.stdout.write(head(f"pull_unauthorized_from_po: {mode}, {len(ids)} client(s) to check\n"))

        candidates = 0
        total_orders = 0
        applied = 0
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
            if _has_open_approved(client):
                continue  # authorized -> leave alone

            hh_ids = _household_client_ids(client)
            future_orders = list(
                DeliveryOrder.objects.filter(
                    member_id__in=hh_ids,
                    expected_delivery_date__gte=today,
                    status__in=_CANCELLABLE_DO_STATUSES,
                ).select_related("purchase_order", "member")
            )
            future_sched = OrderSchedule.objects.filter(
                member__client_id__in=hh_ids,
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
            ).count()
            if not future_orders and not future_sched:
                continue

            candidates += 1
            total_orders += len(future_orders)
            auth = sorted({c.service_authorization_status or "(blank)" for c in isc})
            self.stdout.write(
                f"\n  {client.pk} | {client.first_name} {client.last_name} "
                f"| household={len(hh_ids)} | auth={auth} "
                f"| future_orders={len(future_orders)} | future_scheduled={future_sched}"
            )
            for do in future_orders:
                po = do.purchase_order
                who = f"{do.member.first_name} {do.member.last_name}" if do.member else "?"
                self.stdout.write(
                    f"      - DO {do.pk} {who} date={do.expected_delivery_date} "
                    f"do_status={do.status} PO={getattr(po,'po_number',None)} "
                    f"po_status={getattr(po,'status',None)}"
                )

            if apply:
                with transaction.atomic():
                    if future_orders:
                        DeliveryOrder.objects.filter(
                            pk__in=[d.pk for d in future_orders]
                        ).update(status=DeliveryOrderStatus.CANCELLED)
                    result = reconcile_internal_service_authorization(
                        client, actor_label="Data fix: pull unauthorized household off PO",
                    )
                applied += 1
                self.stdout.write(self.style.WARNING(
                    f"      -> cancelled {len(future_orders)} order(s); reconcile: {result}"
                ))

        self.stdout.write(head(
            f"\nDone. Candidates={candidates}, future_orders={total_orders}, applied={applied}."
        ))
        if not apply and candidates:
            self.stdout.write("Re-run with --apply to cancel the orders and reconcile.")

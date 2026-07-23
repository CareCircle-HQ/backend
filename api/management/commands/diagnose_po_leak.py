"""Diagnose why clients whose internal-service case is closed/cancelled still
appear (or don't) on a Purchase Order.

For each client it prints the internal-service case status(es), the governing
enrollment stage(s), the member dietary-profile status, and any FUTURE
``SCHEDULED`` delivery-calendar occurrences -- then classifies the client into
one of:

  ON_PO_LEAK   -- has a future SCHEDULED occurrence that STILL passes PO
                  candidate selection (enrollment stage not excluded AND member
                  status not excluded). These would wrongly land on a real PO.
                  Sub-classified by whether the internal-service case is closed.
  STAGE_OK     -- enrollment is On Hold / terminal (or member out-of-service),
                  so the reports + PO generation already exclude them. Any
                  lingering SCHEDULED occurrence is harmless (filtered out).
  NO_SCHEDULE  -- no future SCHEDULED occurrence at all.

Read-only: makes no changes.

Usage:
    python manage.py diagnose_po_leak <client_id> [<client_id> ...]
    python manage.py diagnose_po_leak --file ids.txt
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
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from api.services.catalog import product_type_kind_for_name

_CLOSED_CASE_STATUSES = {"closed", "cancelled", "canceled"}


class Command(BaseCommand):
    help = "Classify clients by whether they still qualify for a PO despite a closed case."

    def add_arguments(self, parser):
        parser.add_argument("client_ids", nargs="*", help="One or more client UUIDs.")
        parser.add_argument(
            "--file",
            dest="file",
            help="Path to a file with one client UUID per line (blank lines ignored).",
        )

    def _load_ids(self, opts):
        ids = list(opts.get("client_ids") or [])
        if opts.get("file"):
            with open(opts["file"]) as fh:
                ids += [line.strip() for line in fh if line.strip()]
        # De-dupe, preserve order.
        seen, out = set(), []
        for cid in ids:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    def handle(self, *args, **opts):
        today = timezone.localdate()
        ids = self._load_ids(opts)
        if not ids:
            self.stderr.write("No client ids given. Pass ids or --file.")
            return

        buckets = {
            "ON_PO_LEAK_CASE_CLOSED": [],
            "ON_PO_LEAK_CASE_OPEN": [],
            "STAGE_OK": [],
            "NO_SCHEDULE": [],
            "NOT_FOUND": [],
        }
        head = self.style.MIGRATE_HEADING

        for cid in ids:
            client = (
                Client.objects.filter(pk=cid)
                .prefetch_related("cases", "enrollments", "member_profiles")
                .first()
            )
            self.stdout.write(head(f"\n=== Client {cid} ==="))
            if client is None:
                self.stdout.write(self.style.ERROR("  client not found"))
                buckets["NOT_FOUND"].append(cid)
                continue

            self.stdout.write(f"  name: {client.first_name} {client.last_name}")

            isc = [c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE]
            any_open_case = any(
                (c.case_status or "").lower() not in _CLOSED_CASE_STATUSES for c in isc
            )
            for c in isc:
                kind = product_type_kind_for_name(c.program_name) or "?"
                self.stdout.write(
                    f"  case {c.case_id} | {c.case_status} | {kind} | {c.program_name or '—'}"
                )

            enrollments = list(client.enrollments.all())
            for e in enrollments:
                self.stdout.write(
                    f"  enrollment {e.pk} | stage={e.stage} | closed_at={e.closed_at}"
                )

            profiles = list(client.member_profiles.all())
            for p in profiles:
                self.stdout.write(f"  member_profile status={p.status}")

            future = list(
                OrderSchedule.objects.filter(
                    member__client_id=cid,
                    status=OrderStatus.SCHEDULED,
                    anticipated_delivery_date__gte=today,
                ).select_related("member", "enrollment")
            )
            # A future SCHEDULED occurrence that still passes PO candidate
            # selection = it would appear on a PO / the export.
            leaking = [
                o for o in future
                if (o.enrollment is None
                    or o.enrollment.stage not in SERVICE_EXCLUDED_ENROLLMENT_STAGES)
                and (o.member is None
                     or o.member.status not in SERVICE_EXCLUDED_MEMBER_STATUSES)
            ]
            self.stdout.write(
                f"  future SCHEDULED occurrences: {len(future)} "
                f"(still PO-eligible: {len(leaking)})"
            )
            if leaking:
                nxt = min(o.anticipated_delivery_date for o in leaking)
                stages = sorted({str(o.enrollment.stage) for o in leaking if o.enrollment})
                self.stdout.write(
                    self.style.WARNING(
                        f"    -> LEAK: next {nxt}, enrollment stage(s)={stages}"
                    )
                )

            if leaking:
                key = "ON_PO_LEAK_CASE_OPEN" if any_open_case else "ON_PO_LEAK_CASE_CLOSED"
            elif future:
                key = "STAGE_OK"
            else:
                key = "NO_SCHEDULE"
            buckets[key].append(cid)

        self.stdout.write(head("\n\n=== Summary ==="))
        for key, members in buckets.items():
            self.stdout.write(f"  {key}: {len(members)}")
            for cid in members:
                self.stdout.write(f"      {cid}")

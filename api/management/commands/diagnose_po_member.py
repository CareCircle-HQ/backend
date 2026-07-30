"""Explain why ONE member does / does not appear on the Purchase Order for a
given delivery date.

Reproduces the EXACT ``_due_schedules`` gate chain (see
``api.services.purchase_orders``) for a single member + date and prints the
cumulative count after each gate -- the gate where the count drops to 0 is the
reason the member is excluded. Also dumps the internal-service cases for both
the enrollment APPLICANT (which the case guardrails actually key on) and the
member themselves (in case they are a household dependent).

Read-only: makes no changes.

Usage:
    python manage.py diagnose_po_member <member_client_id> <YYYY-MM-DD>
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from api.models import (
    Case,
    CaseType,
    ClientStage,
    OrderSchedule,
    ScheduleStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from api.services.catalog import (
    product_kind_for_enrollment,
    product_type_kind_for_name,
)
from api.services.purchase_orders import (
    authorized_internal_service_case_exists,
    open_internal_service_case_exists,
)


class Command(BaseCommand):
    help = "Explain why a member is / isn't on the PO for a delivery date."

    def add_arguments(self, parser):
        parser.add_argument("member_client_id", help="Member client UUID.")
        parser.add_argument("delivery_date", help="Delivery date YYYY-MM-DD.")

    def handle(self, *args, **opts):
        mid = opts["member_client_id"]
        try:
            d = date.fromisoformat(opts["delivery_date"])
        except ValueError:
            raise CommandError("delivery_date must be YYYY-MM-DD")

        w = self.stdout.write
        head = self.style.MIGRATE_HEADING

        w(head(f"\n=== Schedule rows for member {mid} on {d} ==="))
        rows = OrderSchedule.objects.filter(
            member__client_id=mid, anticipated_delivery_date=d
        ).select_related("enrollment", "member", "member__client")
        w(f"rows on date: {rows.count()}")
        applicant_ids = set()
        for s in rows:
            enr = s.enrollment
            if enr and enr.client_id:
                applicant_ids.add(enr.client_id)
            client = getattr(s.member, "client", None)
            w(
                f"  order_id={s.order_id} status={s.status} "
                f"| enr={s.enrollment_id} enr.stage={getattr(enr, 'stage', None)} "
                f"enr.client(applicant)={getattr(enr, 'client_id', None)} "
                f"| member.status={getattr(s.member, 'status', None)} "
                f"member.client={getattr(s.member, 'client_id', None)} "
                f"client.lifecycle={getattr(client, 'lifecycle_stage', None)} "
                f"| kitchen={s.kitchen_id} program={s.program_name!r}"
            )

        w(head("\n=== Cumulative gate counts (first 0 is the culprit) ==="))
        b = rows.filter(status=ScheduleStatus.SCHEDULED)
        w(f"1 SCHEDULED: {b.count()}")
        b = b.exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        w(f"2 enrollment stage ok: {b.count()}")
        b = b.exclude(member__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        w(f"3 member status ok: {b.count()}")
        b = b.exclude(member__client__lifecycle_stage=ClientStage.INELIGIBLE)
        w(f"4 not INELIGIBLE: {b.count()}")
        b = b.annotate(_o=open_internal_service_case_exists()).filter(_o=True)
        w(f"5 open ISC (keyed on enrollment.client / applicant): {b.count()}")
        b = b.annotate(_a=authorized_internal_service_case_exists()).filter(_a=True)
        w(f"6 authorized ISC: {b.count()}")
        for s in b.select_related("enrollment"):
            k = product_type_kind_for_name(s.program_name) or product_kind_for_enrollment(
                s.enrollment
            )
            w(f"7 resolved product kind: {k}")

        w(head("\n=== Internal-service cases per APPLICANT (drives gates 5/6) ==="))
        for cid in (applicant_ids or {mid}):
            w(f"  applicant: {cid}")
            for c in Case.objects.filter(
                client_id=cid, case_type=CaseType.INTERNAL_SERVICE
            ):
                self._dump_case(c)

        w(head("\n=== This member's OWN internal-service cases (for comparison) ==="))
        for c in Case.objects.filter(client_id=mid, case_type=CaseType.INTERNAL_SERVICE):
            self._dump_case(c)

    def _dump_case(self, c):
        # Field names differ between deployments; print whatever exists.
        starts = (
            getattr(c, "service_authorization_approval_starts_at", None)
            or getattr(c, "authorized_starts_at", None)
        )
        ends = (
            getattr(c, "service_authorization_approval_ends_at", None)
            or getattr(c, "authorized_ends_at", None)
        )
        self.stdout.write(
            f"    case={c.case_id} status={c.case_status} "
            f"auth={c.service_authorization_status} "
            f"window={starts}->{ends} program={c.program_name!r}"
        )

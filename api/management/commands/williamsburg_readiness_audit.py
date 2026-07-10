"""Read-only audit: which Williamsburg clients are NOT truly ready to serve.

After ``reconcile_williamsburg_revised --assign-missing-kitchen`` a client can be
Service Active yet have an EMPTY delivery calendar (0 OrderSchedule rows) --
almost always because the internal-service case has no authorization approval
window (``service_authorization_approval_starts_at`` / ``_ends_at``). Once a
client is Service Active + kitchen-assigned the reconcile command treats it as
``OK``, so these silently drop off its radar. This command surfaces them.

For every ``is_williamsburg`` client (or just the ids in ``--file``) it inspects
the latest enrollment and reports one bucket per client:

  * ``no_enrollment``      - Williamsburg client with no enrollment at all.
  * ``not_active``         - enrollment exists but not Service Active (On Hold /
                             kitchen_assignment / pending / cancelled ...).
  * ``no_calendar``        - Service Active but 0 calendar orders. NOT ready to
                             serve. Shows whether the case auth window is missing.
  * ``ready``              - Service Active with a delivery calendar (counted).

Read-only: never writes. Dry-run has no meaning here.

Usage:
    python manage.py williamsburg_readiness_audit
    python manage.py williamsburg_readiness_audit --file /path/to/list.xlsx
"""
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand

from api.models import Client, EnrollmentStage


def _norm(value):
    return "" if value is None else str(value).strip()


def _read_ids(path):
    """Ordered unique lowercase client ids from the first-column sheet."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    header = [_norm(c) for c in rows[0]]
    try:
        ci = header.index("Unite Us Client ID")
    except ValueError:
        ci = 0
    seen, out = set(), []
    for r in rows[1:]:
        cid = _norm(r[ci]).lower() if ci < len(r) else ""
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _case_has_auth_window(case):
    if case is None:
        return False
    return bool(
        getattr(case, "service_authorization_approval_starts_at", None)
        and getattr(case, "service_authorization_approval_ends_at", None)
    )


class Command(BaseCommand):
    help = (
        "Read-only: report Williamsburg clients that are Service Active but have "
        "no delivery calendar (not ready to serve), plus their case auth-window "
        "status."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", default="",
            help="Optional .xlsx to restrict the audit to (else all is_williamsburg clients).",
        )

    def handle(self, *args, **options):
        path = options["file"]
        if path:
            ids = _read_ids(path)
            clients = [Client.objects.filter(client_id=cid).first() for cid in ids]
            clients = [c for c in clients if c is not None]
            source = f"{path} ({len(clients)} of {len(ids)} ids in DB)"
        else:
            clients = list(Client.objects.filter(is_williamsburg=True))
            source = f"all is_williamsburg clients ({len(clients)})"

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Williamsburg readiness audit: {source}"
        ))

        report = Counter()
        no_calendar = []   # (cid, note)
        not_active = []     # (cid, stage)
        no_enrollment = []  # cid

        for client in clients:
            cid = str(client.client_id)
            enr = client.enrollments.order_by("-opened_at").first()
            if enr is None:
                report["no_enrollment"] += 1
                no_enrollment.append(cid)
                continue
            if enr.stage != EnrollmentStage.SERVICE_ACTIVE:
                report["not_active"] += 1
                not_active.append((cid, enr.stage))
                continue

            n_orders = enr.orders.count()
            if n_orders == 0:
                report["no_calendar"] += 1
                has_window = _case_has_auth_window(enr.case)
                cause = (
                    "case has NO authorization window (set starts_at/ends_at)"
                    if not has_window else
                    "auth window present but 0 orders (regenerate calendar)"
                )
                no_calendar.append((
                    cid,
                    f"{enr.delivery_schedules.count()} schedule(s), 0 orders -- {cause}",
                ))
            else:
                report["ready"] += 1

        # --- NOT ready to serve: the punch-list -----------------------------
        self.stdout.write(self.style.ERROR(
            f"\n=== Service Active but NO calendar (NOT ready) -- {len(no_calendar)} ==="
        ))
        for cid, note in no_calendar:
            self.stdout.write(f"  {cid}: {note}")

        if no_enrollment:
            self.stdout.write(self.style.WARNING(
                f"\n=== Williamsburg clients with NO enrollment -- {len(no_enrollment)} ==="
            ))
            for cid in no_enrollment:
                self.stdout.write(f"  {cid}")

        if not_active:
            self.stdout.write(self.style.WARNING(
                f"\n=== Enrolled but NOT Service Active -- {len(not_active)} ==="
            ))
            for cid, stage in not_active:
                self.stdout.write(f"  {cid}: {stage}")

        # --- Stats ----------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Stats ==="))
        stats = [
            ("Clients audited", len(clients)),
            ("Ready (Service Active + calendar)", report["ready"]),
            ("Service Active but NO calendar", report["no_calendar"]),
            ("Enrolled but not Service Active", report["not_active"]),
            ("No enrollment", report["no_enrollment"]),
        ]
        for label, value in stats:
            self.stdout.write(f"  {label:<38}: {value}")

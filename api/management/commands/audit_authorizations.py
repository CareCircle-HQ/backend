"""Read-only diagnostic: for every enrollment past verification, report the
authorization status of the CASE that governs it.

This answers "why is this household still Waiting Authorization even though it has
members + a delivery address?". A verified household sits at Waiting Authorization
until its governing internal-service case carries an *approved* (or not-required)
authorization status -- a blank / pending status keeps it parked there. This
command shows, per enrollment stage, the distribution of the governing case's
authorization status, and flags every AFFECTED household -- one whose governing
internal-service case is approved but whose enrollment still reads Waiting
Authorization OR Denied (so reconcile_authorizations --apply would move it to
Kitchen Assignment).

It writes NOTHING. Run it on prod to see the real picture:

    python manage.py audit_authorizations            # summary only
    python manage.py audit_authorizations --details   # + per-household lines
    python manage.py audit_authorizations --affected   # only the affected households

The companion writer is ``reconcile_authorizations`` (use --apply there to fix
the stuck ones).
"""
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import (
    _AUTH_STATUS_TO_STAGE,
    governing_internal_case,
)

# Verified enrollments -- the only ones whose case authorization is actionable
# (mirrors reconcile_authorizations.ELIGIBLE_STAGES).
ELIGIBLE_STAGES = [
    EnrollmentStage.VERIFIED,
]


class Command(BaseCommand):
    help = (
        "Read-only: report the governing case authorization status for "
        "verified/waiting/denied enrollments, and flag households stuck at "
        "Waiting Authorization despite an approved case."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--details", action="store_true",
            help="List every enrollment (client, household size, address, case, status).",
        )
        parser.add_argument(
            "--stuck", "--affected", dest="stuck", action="store_true",
            help="Only show AFFECTED households: approved governing case but still "
                 "reading Waiting Authorization or Denied (would advance on reconcile).",
        )

    def handle(self, *args, **opts):
        qs = (
            EnrollmentVerification.objects.filter(stage__in=ELIGIBLE_STAGES)
            .select_related("client", "household", "delivery_address")
            .order_by("stage", "id")
        )

        # stage -> Counter(auth_status)
        by_stage = defaultdict(Counter)
        # (stage, auth_status) -> projected target stage
        affected_rows = []
        detail_rows = []
        total = 0

        for enr in qs.iterator():
            total += 1
            case = governing_internal_case(enr)
            auth = (case.service_authorization_status or "(blank)") if case else "(no internal case)"
            by_stage[enr.stage][auth] += 1

            # Would reconcile move it? Only an approval advances the stage.
            target = None
            if case is not None:
                target = _AUTH_STATUS_TO_STAGE.get(case.service_authorization_status)
            # "Affected": the governing internal-service case is APPROVED
            # (target == Kitchen Assignment) but the enrollment is still VERIFIED
            # -- reconcile_authorizations --apply advances it to Kitchen
            # Assignment.
            is_affected = (
                target == EnrollmentStage.KITCHEN_ASSIGNMENT
                and enr.stage == EnrollmentStage.VERIFIED
            )

            hh = enr.household
            hh_size = hh.members.count() if hh is not None else 0
            has_addr = bool(enr.delivery_address_id)
            client = enr.client
            name = (
                f"{client.first_name} {client.last_name}".strip()
                if client else "(no client)"
            )
            row = {
                "code": enr.code or str(enr.id),
                "name": name,
                "client_id": str(client.client_id) if client else "",
                "stage": enr.stage,
                "hh_size": hh_size,
                "addr": "addr" if has_addr else "NO-ADDR",
                "case_id": str(case.case_id) if case else "",
                "auth": auth,
                "would_move_to": target if (target and target != enr.stage) else "",
            }
            if is_affected:
                affected_rows.append(row)
            if opts["details"]:
                detail_rows.append(row)

        # ---- Summary -------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{total} enrollment(s) past verification (verified / waiting / denied)\n"
        ))
        for stage in ELIGIBLE_STAGES:
            counter = by_stage.get(stage)
            if not counter:
                continue
            stage_total = sum(counter.values())
            self.stdout.write(self.style.HTTP_INFO(f"{stage}  ({stage_total})"))
            for auth, n in counter.most_common():
                self.stdout.write(f"    {n:6d}  case auth = {auth}")
            self.stdout.write("")

        # ---- Affected flag -------------------------------------------------
        if affected_rows:
            by_cur = Counter(r["stage"] for r in affected_rows)
            breakdown = ", ".join(f"{n} {st}" for st, n in by_cur.most_common())
            self.stdout.write(self.style.WARNING(
                f"{len(affected_rows)} household(s) AFFECTED: the governing internal-"
                f"service case is APPROVED but the enrollment still reads Waiting "
                f"Authorization / Denied ({breakdown}) -> run "
                f"'reconcile_authorizations --apply' to advance them to Kitchen "
                f"Assignment.\n"
            ))
            for r in affected_rows:
                self.stdout.write(
                    f"    {r['code']:>12}  {r['stage']:<22}  {r['name'][:26]:<26}  "
                    f"hh={r['hh_size']}  {r['addr']:<8}  case={r['case_id']}"
                )
            self.stdout.write("")
        else:
            self.stdout.write(self.style.SUCCESS(
                "No affected households: every Waiting-Authorization / Denied enrollment "
                "has a non-approved (blank/pending/denied/expired) governing case, so its "
                "status is correct.\n"
            ))

        # ---- Optional per-household detail ---------------------------------
        rows = affected_rows if opts["stuck"] else detail_rows
        if rows and not (opts["stuck"] and affected_rows):
            self.stdout.write(self.style.MIGRATE_HEADING("Detail:"))
            for r in rows:
                move = f"  => {r['would_move_to']}" if r["would_move_to"] else ""
                self.stdout.write(
                    f"    {r['code']:>12}  {r['stage']:<22}  {r['name'][:26]:<26}  "
                    f"hh={r['hh_size']}  {r['addr']:<8}  auth={r['auth']}{move}"
                )

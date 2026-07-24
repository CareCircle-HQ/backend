"""Fleet-wide delivery-calendar rebuild driven by authorization status.

The whole-population counterpart to ``backfill_delivery_calendar`` (which is
scoped to explicit client ids). It finds every serviceable enrollment whose
delivery plan window (``MemberDeliverySchedule.starts_on/ends_on``) has fallen
SHORT of its governing internal-service authorization -- i.e. the case is still
APPROVED (or Not Required) with an authorization window reaching into the
future, but the plan window ended earlier, so the dated calendar
(``OrderSchedule``) has no upcoming occurrences and the member silently drops
off every Purchase Order.

For each such enrollment it EXTENDS the plan window (never shortens it) to the
authorization end, then reconciles the calendar via ``sync_delivery_calendar``
(which only adds/refreshes FUTURE occurrences and never disturbs anything
already batched into a PO).

Gating is strictly by AUTHORIZATION:
  * enrollment stage must not be service-excluded (On Hold / terminal);
  * the governing internal-service case must be APPROVED / Not Required;
  * its authorization window must end on/after ``--from-date`` (a lapsed
    authorization needs re-authorization, not a calendar rebuild -- skipped).

Dry-run by default -- prints what it would do. Pass ``--apply`` to persist.

Usage::

    # preview everything (no writes)
    python manage.py rebuild_authorized_calendars

    # preview a single kitchen
    python manage.py rebuild_authorized_calendars --kitchen Williamsburg

    # apply
    python manage.py rebuild_authorized_calendars --apply
    python manage.py rebuild_authorized_calendars --kitchen Williamsburg --apply
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import (
    EnrollmentVerification,
    ScheduleStatus,
    ServiceAuthorizationStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from api.services.lifecycle import governing_internal_case
from api.services.orders import sync_delivery_calendar

# Authorization statuses that permit delivery generation (an approval or an
# explicit "not required"). Anything else (pending/denied/expired/blank) must
# NOT cause the window to be extended.
_AUTHORIZED_STATUSES = {
    ServiceAuthorizationStatus.APPROVED,
    ServiceAuthorizationStatus.NOT_REQUIRED,
}


def _auth_end_date(case):
    """The governing case's authorization approval END as a date, or None."""
    ends_at = getattr(case, "service_authorization_approval_ends_at", None)
    return ends_at.date() if ends_at else None


class Command(BaseCommand):
    help = (
        "Extend every serviceable enrollment's delivery plan window to its "
        "governing (approved) authorization end and regenerate the future "
        "delivery calendar. Selection is by authorization status. Dry-run "
        "unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this the command only reports.",
        )
        parser.add_argument(
            "--kitchen",
            default=None,
            help="Limit to enrollments whose assigned kitchen name contains "
            "this text (case-insensitive), e.g. 'Williamsburg'.",
        )
        parser.add_argument(
            "--from-date",
            dest="from_date",
            default=None,
            help="Only (re)generate occurrences on/after this date "
            "(YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most N enrollments (useful for a bounded trial).",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print a line for every enrollment, including skips.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        verbose = opts["verbose"]
        from_date = timezone.localdate()
        if opts["from_date"]:
            try:
                from_date = datetime.strptime(opts["from_date"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--from-date must be YYYY-MM-DD")

        mode = self.style.SUCCESS("APPLY") if apply else self.style.WARNING("DRY-RUN")
        scope = f" | kitchen~='{opts['kitchen']}'" if opts["kitchen"] else ""
        self.stdout.write(f"Mode: {mode} | from-date: {from_date}{scope}\n")

        qs = (
            EnrollmentVerification.objects.filter(
                delivery_schedules__status=ScheduleStatus.SCHEDULED,
            )
            .exclude(stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
            .distinct()
            .select_related("case", "kitchen")
            .order_by("pk")
        )
        if opts["kitchen"]:
            qs = qs.filter(kitchen__name__icontains=opts["kitchen"])
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        totals = {
            "seen": 0, "eligible": 0, "plans_extended": 0,
            "added": 0, "removed": 0, "updated": 0,
        }
        skips = {}
        for enr in qs:
            totals["seen"] += 1
            res = self._process_enrollment(enr, from_date, apply, verbose)
            if res.get("skip"):
                skips[res["skip"]] = skips.get(res["skip"], 0) + 1
                continue
            totals["eligible"] += 1
            totals["plans_extended"] += res["extended"]
            totals["added"] += res["added"]
            totals["removed"] += res["removed"]
            totals["updated"] += res["updated"]

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. enrollments_seen={totals['seen']} "
                f"eligible={totals['eligible']} "
                f"plans_extended={totals['plans_extended']} "
                f"occurrences_added={totals['added']} "
                f"removed={totals['removed']} updated={totals['updated']}"
            )
        )
        if skips:
            self.stdout.write("Skipped: " + ", ".join(
                f"{k}={v}" for k, v in sorted(skips.items())
            ))
        if not apply:
            self.stdout.write(
                self.style.WARNING("No changes written. Re-run with --apply to persist.")
            )

    # -- per-enrollment ----------------------------------------------------
    def _process_enrollment(self, enr, from_date, apply, verbose):
        out = {"extended": 0, "added": 0, "removed": 0, "updated": 0}

        # Only enrollments that actually carry a delivery PLAN are in scope.
        plans = list(
            enr.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED)
            .select_related("member_profile")
        )
        if not plans:
            return {"skip": "no_plans", **out}

        gov = governing_internal_case(enr)
        if gov is None:
            return {"skip": "no_governing_case", **out}
        if gov.service_authorization_status not in _AUTHORIZED_STATUSES:
            return {"skip": "not_authorized", **out}
        auth_end = _auth_end_date(gov)
        if auth_end is None:
            return {"skip": "no_auth_end", **out}
        if auth_end < from_date:
            return {"skip": "authorization_lapsed", **out}

        # Which non-excluded plans have a window short of the authorization?
        to_extend = [
            p for p in plans
            if not (
                p.member_profile is not None
                and p.member_profile.status in SERVICE_EXCLUDED_MEMBER_STATUSES
            )
            and (p.ends_on is None or auth_end > p.ends_on)
        ]
        if not to_extend:
            return {"skip": "already_current", **out}

        if verbose or not apply:
            who = f"{enr.client.first_name} {enr.client.last_name}".strip() if enr.client_id else enr.pk
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"enrollment {enr.pk} ({who}) | stage {enr.stage} | "
                f"case {gov.case_id} auth={gov.service_authorization_status} "
                f"window_end={auth_end}"
            ))
            for p in to_extend:
                self.stdout.write(f"    plan {p.pk}: extend ends_on {p.ends_on} -> {auth_end}")

        out["extended"] = len(to_extend)
        if not apply:
            self.stdout.write("    (dry-run) would sync_delivery_calendar()")
            return out

        with transaction.atomic():
            for p in to_extend:
                p.ends_on = auth_end
                p.save(update_fields=["ends_on", "updated_at"])
            res = sync_delivery_calendar(enr, from_date=from_date)
        out["added"] = res["added"]
        out["removed"] = res["removed"]
        out["updated"] = res["updated"]
        if verbose:
            self.stdout.write(self.style.SUCCESS(
                f"    synced: added={res['added']} removed={res['removed']} "
                f"updated={res['updated']}"
            ))
        return out

"""Targeted delivery-calendar backfill for SPECIFIC members.

Fixes the "active member missing from every Purchase Order" case where a
household's delivery plan window (``MemberDeliverySchedule.starts_on/ends_on``,
snapshotted from the case authorization at plan-build time) has ELAPSED, so the
dated calendar (``OrderSchedule``) has no future occurrences left to batch --
even though the governing internal-service case is still authorized (approved
with an authorization window that extends into the future).

Scope is deliberately narrow: it acts ONLY on the client ids you pass, and only
on the enrollments those clients belong to. It never touches any other
household. It EXTENDS a plan window (never shortens it) to the governing case's
current authorization end, then reconciles the calendar via
``sync_delivery_calendar`` (which only adds/refreshes FUTURE occurrences and
never disturbs anything already batched into a PO).

Dry-run by default -- prints exactly what it would do. Pass ``--apply`` to
persist.

Usage::

    # preview (no writes)
    python manage.py backfill_delivery_calendar 7d5dfa91-e31c-4a80-99c3-94000f99acb0

    # apply
    python manage.py backfill_delivery_calendar <client_id> [<client_id> ...] --apply
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import (
    Client,
    EnrollmentVerification,
    MemberDietaryProfile,
    OrderSchedule,
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
        "Extend the delivery plan window from the governing (approved) "
        "internal-service authorization and regenerate the delivery calendar "
        "for the given client id(s). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "client_ids",
            nargs="+",
            help="One or more Client ids (UUIDs) to backfill.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this the command only reports.",
        )
        parser.add_argument(
            "--from-date",
            dest="from_date",
            default=None,
            help="Only (re)generate occurrences on/after this date "
            "(YYYY-MM-DD). Defaults to today.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        from_date = timezone.localdate()
        if opts["from_date"]:
            try:
                from_date = datetime.strptime(opts["from_date"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--from-date must be YYYY-MM-DD")

        mode = self.style.SUCCESS("APPLY") if apply else self.style.WARNING("DRY-RUN")
        self.stdout.write(f"Mode: {mode} | from-date: {from_date}\n")

        totals = {"clients": 0, "enrollments": 0, "extended_plans": 0, "added": 0}
        for raw in opts["client_ids"]:
            cid = raw.strip()
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Client {cid} ==="))
            client = Client.objects.filter(pk=cid).first()
            if client is None:
                self.stdout.write(self.style.ERROR("  not found -- skipped"))
                continue
            totals["clients"] += 1
            self.stdout.write(
                f"  {client.first_name} {client.last_name}".rstrip()
            )

            before = self._future_scheduled(cid, from_date)
            self.stdout.write(f"  future SCHEDULED before: {before}")

            for enr in self._enrollments_for(cid):
                res = self._process_enrollment(enr, from_date, apply)
                totals["enrollments"] += 1
                totals["extended_plans"] += res["extended"]
                totals["added"] += res["added"]

            after = self._future_scheduled(cid, from_date)
            self.stdout.write(
                f"  future SCHEDULED after:  {after}"
                + ("" if apply else "  (dry-run -- unchanged)")
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. clients={totals['clients']} "
                f"enrollments={totals['enrollments']} "
                f"plans_extended={totals['extended_plans']} "
                f"occurrences_added={totals['added']}"
            )
        )
        if not apply:
            self.stdout.write(
                self.style.WARNING("No changes written. Re-run with --apply to persist.")
            )

    # -- helpers ------------------------------------------------------------
    def _enrollments_for(self, cid):
        """Enrollments this client participates in (via a dietary profile),
        unioned with the client's own enrollments."""
        enr_ids = set(
            MemberDietaryProfile.objects.filter(client_id=cid)
            .values_list("enrollment_id", flat=True)
        )
        enr_ids |= set(
            EnrollmentVerification.objects.filter(client_id=cid)
            .values_list("pk", flat=True)
        )
        enr_ids.discard(None)
        return (
            EnrollmentVerification.objects.filter(pk__in=enr_ids)
            .select_related("client", "case")
        )

    def _future_scheduled(self, cid, from_date):
        return OrderSchedule.objects.filter(
            member__client_id=cid,
            status=ScheduleStatus.SCHEDULED,
            anticipated_delivery_date__gte=from_date,
        ).count()

    def _process_enrollment(self, enr, from_date, apply):
        out = {"extended": 0, "added": 0}
        self.stdout.write(f"  enrollment {enr.pk} | stage {enr.stage}")

        if str(enr.stage) in {str(s) for s in SERVICE_EXCLUDED_ENROLLMENT_STAGES}:
            self.stdout.write(
                self.style.WARNING(
                    f"    stage '{enr.stage}' excludes service -- skipped"
                )
            )
            return out

        # Only enrollments that actually carry a delivery PLAN are in scope. A
        # pre-service enrollment (pending/verified) has none, so there is nothing
        # to extend or regenerate -- skip it cleanly.
        plans = list(
            enr.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED)
            .select_related("member_profile")
        )
        if not plans:
            self.stdout.write("    no delivery plans -- skipped")
            return out

        gov = governing_internal_case(enr)
        if gov is None:
            self.stdout.write(self.style.WARNING("    no governing case -- skipped"))
            return out
        status = gov.service_authorization_status or "(blank)"
        auth_end = _auth_end_date(gov)
        self.stdout.write(
            f"    governing case {gov.case_id} | auth={status} | window_end={auth_end}"
        )

        if gov.service_authorization_status not in _AUTHORIZED_STATUSES:
            self.stdout.write(
                self.style.WARNING("    authorization not approved -- skipped")
            )
            return out
        if auth_end is None:
            self.stdout.write(
                self.style.WARNING("    no authorization end date -- skipped")
            )
            return out
        if auth_end < from_date:
            self.stdout.write(
                self.style.WARNING(
                    f"    authorization ended {auth_end} (before {from_date}) -- "
                    "needs re-authorization, not a calendar issue -- skipped"
                )
            )
            return out

        # Extend (never shorten) each scheduled plan window to the authorization
        # end so the reconcile can generate the missing future occurrences.
        for p in plans:
            excluded = (
                p.member_profile is not None
                and p.member_profile.status in SERVICE_EXCLUDED_MEMBER_STATUSES
            )
            if excluded:
                continue
            if p.ends_on is None or auth_end > p.ends_on:
                self.stdout.write(
                    f"      plan {p.pk}: extend ends_on {p.ends_on} -> {auth_end}"
                )
                out["extended"] += 1
                if apply:
                    p.ends_on = auth_end
                    p.save(update_fields=["ends_on", "updated_at"])

        if not apply:
            self.stdout.write("    (dry-run) would sync_delivery_calendar()")
            return out

        with transaction.atomic():
            res = sync_delivery_calendar(enr, from_date=from_date)
        out["added"] = res["added"]
        self.stdout.write(
            self.style.SUCCESS(
                f"    synced: added={res['added']} removed={res['removed']} "
                f"updated={res['updated']}"
            )
        )
        return out

"""One-off data fix (2026-07-23): re-anchor Eli Mittelman's enrollment to her
OWN household and activate it, so she generates a delivery calendar and lands on
a Williamsburg Purchase Order.

Background
----------
Eli (``316078bd-...``) holds her own APPROVED internal-service case, so she
should be the head of her own household with her own active enrollment. Instead:

  * her case-linked enrollment sits at ``pending_verification`` and is anchored
    to a DIFFERENT (shared) household -- so no delivery calendar was ever built
    and she never reached a Purchase Order;
  * because it was created while she was grouped as a dependent, its
    ``MemberDietaryProfile`` is the OTHER member's, not hers.

The fix re-anchors that ONE enrollment (the one linked to her open
internal-service case) to the household where she is primary, then runs the
Williamsburg fast-track, which rebuilds her dietary profile from her own
household roster, assigns the Williamsburg kitchen, builds the delivery
schedule + dated calendar, and advances the enrollment to Service Active.

Scope guards
------------
Only the enrollment linked to Eli's OPEN internal-service case is touched. Any
OTHER enrollment on her client record (e.g. a caseless enrollment that carries a
different member's profile and is serving that member) is LEFT ALONE -- it is a
separate tangle that must be resolved on its own.

Dry-run by default (prints the before/after plan). Pass ``--apply`` to commit.
Idempotent: if the enrollment is already Service Active on her own household with
her own profile, it reports "already fixed" and makes no changes. Runs in one
transaction. Safe to delete this command once run on prod.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import (
    Case,
    CaseStatus,
    CaseType,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    OrderSchedule,
    ScheduleStatus,
)

# Eli Mittelman. Stable across environments (client UUID is the same in prod).
ELI_CLIENT_ID = "316078bd-ed2c-41a2-b2e6-7177c7d78ee8"

_CLOSED_CASE_STATUSES = (CaseStatus.CLOSED, CaseStatus.CANCELLED)


class Command(BaseCommand):
    help = (
        "Re-anchor Eli Mittelman's case-linked enrollment to her own household "
        "and activate it (Williamsburg fast-track). Dry-run by default; pass "
        "--apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply. Without this the command only previews.",
        )
        parser.add_argument(
            "--client-id", default=ELI_CLIENT_ID,
            help="Override the client UUID to repair (defaults to Eli).",
        )

    # -- helpers -----------------------------------------------------------
    def _own_household(self, client):
        """The household where this client is the PRIMARY (head), or None."""
        hm = (
            HouseholdMember.objects
            .filter(client=client, is_primary=True)
            .select_related("household")
            .first()
        )
        return hm.household if hm else None

    def _governing_case(self, client):
        """The client's OPEN (not closed/cancelled) internal-service case,
        preferring an APPROVED one. None when there isn't one."""
        cases = list(
            Case.objects
            .filter(client=client, case_type=CaseType.INTERNAL_SERVICE)
            .exclude(case_status__in=_CLOSED_CASE_STATUSES)
        )
        if not cases:
            return None
        cases.sort(key=lambda c: c.service_authorization_status == "approved", reverse=True)
        return cases[0]

    def _scheduled_count(self, client):
        return OrderSchedule.objects.filter(
            member__client=client, status=ScheduleStatus.SCHEDULED
        ).count()

    # -- main --------------------------------------------------------------
    def handle(self, *args, **opts):
        apply = opts["apply"]
        client_id = opts["client_id"]

        try:
            client = Client.objects.get(client_id=client_id)
        except Client.DoesNotExist:
            raise CommandError(f"Client {client_id} not found.")

        name = f"{client.first_name} {client.last_name}".strip()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Fix target: {name} ({str(client.client_id)[:8]})"
        ))

        if not getattr(client, "is_williamsburg", False):
            raise CommandError(
                f"{name} is not a Williamsburg client (lead_source="
                f"{client.lead_source!r}); this command applies the Williamsburg "
                "fast-track and must not be used for a non-Williamsburg client."
            )

        own_hh = self._own_household(client)
        if own_hh is None:
            # The fast-track will create/find it, but we must re-anchor to a
            # concrete household BEFORE calling it (it only sets household when
            # the enrollment has none). Create it deterministically here.
            from api.serializers import ensure_household_with_primary
            if apply:
                own_hh = ensure_household_with_primary(client)
            else:
                self.stdout.write(
                    "  own household: NONE yet -- would be created "
                    "(client made primary of a fresh household)."
                )

        gov_case = self._governing_case(client)
        if gov_case is None:
            raise CommandError(
                f"{name} has no OPEN internal-service case -- nothing to anchor "
                "an active enrollment to. Aborting."
            )

        target = (
            EnrollmentVerification.objects
            .filter(client=client, case=gov_case)
            .order_by("opened_at", "pk")
            .first()
        )
        if target is None:
            # Show her enrollments so a human can see why none is case-linked.
            self.stdout.write(self.style.ERROR(
                "No enrollment is linked to her open internal-service case "
                f"{str(gov_case.case_id)[:8]}. Her enrollments:"
            ))
            for e in EnrollmentVerification.objects.filter(client=client):
                self.stdout.write(
                    f"    enr {e.pk} stage={e.stage} case={e.case_id} "
                    f"hh={e.household_id} kitchen={e.kitchen_id}"
                )
            raise CommandError("No case-linked enrollment to repair.")

        # Report the current state + the untouched siblings (scope transparency).
        self.stdout.write(
            f"  governing case: {str(gov_case.case_id)[:8]} "
            f"status={gov_case.case_status} auth={gov_case.service_authorization_status}"
        )
        self.stdout.write(
            f"  target enrollment: {target.pk} stage={target.stage} "
            f"hh={target.household_id} kitchen={target.kitchen_id}"
        )
        self.stdout.write(
            f"  target household -> {own_hh.household_id if own_hh else '(new)'}"
        )
        siblings = EnrollmentVerification.objects.filter(client=client).exclude(pk=target.pk)
        for e in siblings:
            self.stdout.write(self.style.WARNING(
                f"  NOT touched: enr {e.pk} stage={e.stage} case={e.case_id} "
                f"hh={e.household_id} kitchen={e.kitchen_id}"
            ))

        # Idempotency: already fixed?
        already = (
            target.stage == EnrollmentStage.SERVICE_ACTIVE
            and own_hh is not None
            and str(target.household_id) == str(own_hh.household_id)
            and target.member_profiles.filter(client=client).exists()
            and self._scheduled_count(client) > 0
        )
        if already:
            self.stdout.write(self.style.SUCCESS(
                "\nAlready fixed -- enrollment is Service Active on her own "
                f"household with her own profile and {self._scheduled_count(client)} "
                "scheduled deliveries. No changes made."
            ))
            return

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run -- no changes made. Re-run with --apply to: re-anchor "
                f"enrollment {target.pk} to her own household, rebuild her dietary "
                "profile, assign the Williamsburg kitchen, build the delivery "
                "calendar, and advance to Service Active."
            ))
            return

        from api.services.williamsburg import fast_track_williamsburg_enrollment

        with transaction.atomic():
            e = EnrollmentVerification.objects.select_for_update().get(pk=target.pk)
            e.household = own_hh
            e.save(update_fields=["household"])
            e = fast_track_williamsburg_enrollment(e)

        e.refresh_from_db()
        profiles = [
            (str(p.client_id)[:8], p.member_name, p.menu_type, p.status)
            for p in e.member_profiles.all()
        ]
        sched = self._scheduled_count(client)
        next_dates = sorted(set(
            str(d) for d in OrderSchedule.objects.filter(
                member__client=client, status=ScheduleStatus.SCHEDULED,
                anticipated_delivery_date__gte=timezone.localdate(),
            ).values_list("anticipated_delivery_date", flat=True)
        ))[:4]

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied. Enrollment {e.pk}: stage={e.stage} "
            f"hh={e.household_id} kitchen={e.kitchen_id}\n"
            f"  profiles={profiles}\n"
            f"  scheduled deliveries={sched} next={next_dates}"
        ))
        if sched == 0:
            self.stdout.write(self.style.ERROR(
                "  WARNING: no scheduled deliveries were generated -- check the "
                "case authorization window."
            ))

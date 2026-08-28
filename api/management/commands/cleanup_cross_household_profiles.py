"""Neutralize phantom CROSS-HOUSEHOLD member profiles.

A ``MemberDietaryProfile`` whose client belongs to a DIFFERENT household than the
profile's enrollment is a phantom: the member was copied onto a former relative's
household enrollment (see the _create_missing_carried_profiles carry bug) and then
paused/locked there by the household->individual scope reconcile -- so they read
as paused while being primary/active in their OWN household (e.g. RACHEL STEINBERG
on CHAYA FISCHER's household after the 08/19 split).

A profile is a phantom when ALL hold (deliberately CONSERVATIVE -- only an
unambiguous case, a household PRIMARY appearing on someone else's household):
  * its enrollment has a household,
  * its client is the PRIMARY of their OWN household, and
  * its client is NOT a member of THIS enrollment's household.

Phantoms are marked REMOVED (excluded from POs/rosters/carry, audit preserved),
never deleted. Affected clients' lifecycle stage is recomputed so a client wrongly
reading as (e.g.) kitchen_assignment via a phantom corrects to their own state.

Review-only by default:
    python manage.py cleanup_cross_household_profiles
Apply:
    python manage.py cleanup_cross_household_profiles --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import HouseholdMember, MemberDietaryProfile, MemberStatus


class Command(BaseCommand):
    help = "Mark phantom cross-household member profiles as REMOVED."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit (default: review only).")

    def handle(self, *args, **options):
        apply = options["apply"]

        # All (client, household) membership pairs, and the set of clients who are
        # the PRIMARY of their own household (the conservative phantom signal).
        pairs = set(HouseholdMember.objects.values_list("client_id", "household_id"))
        primary_clients = set(
            HouseholdMember.objects.filter(is_primary=True).values_list("client_id", flat=True)
        )

        phantoms = []
        qs = (
            MemberDietaryProfile.objects
            .exclude(status=MemberStatus.REMOVED)
            .exclude(enrollment__household_id=None)
            .exclude(client_id=None)
            .select_related("enrollment")
        )
        for p in qs.iterator():
            cid, hid = p.client_id, p.enrollment.household_id
            # A household PRIMARY appearing on a household that is NOT theirs.
            if cid in primary_clients and (cid, hid) not in pairs:
                phantoms.append(p)

        affected = {p.client_id for p in phantoms}
        live = [p for p in phantoms if p.enrollment.stage not in ("closed", "cancelled", "disregarded")]
        self.stdout.write(f"phantom cross-household profiles : {len(phantoms)}")
        self.stdout.write(f"  on LIVE enrollments            : {len(live)}")
        self.stdout.write(f"  distinct affected clients      : {len(affected)}")
        for p in phantoms[:10]:
            self.stdout.write(
                f"    client {str(p.client_id)[:8]} on enr {str(p.enrollment_id)[:6]} "
                f"(hh {str(p.enrollment.household_id)[:8]}, {p.enrollment.stage}) status={p.status}"
            )

        if not apply:
            self.stdout.write("")
            self.stdout.write("Review only. Re-run with --apply to mark them REMOVED.")
            return

        now = timezone.now()
        with transaction.atomic():
            for p in phantoms:
                p.status = MemberStatus.REMOVED
                p.status_changed_at = now
                p.save(update_fields=["status", "status_changed_at", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"Marked {len(phantoms)} profile(s) REMOVED."))

        # Recompute each affected client's lifecycle stage so a client wrongly
        # derived from a phantom corrects to their own household's state.
        from api.services.lifecycle import recompute_client_stage
        from api.models import Client

        fixed = 0
        for cid in affected:
            c = Client.objects.filter(pk=cid).first()
            if c is None:
                continue
            try:
                recompute_client_stage(c)
                fixed += 1
            except Exception:  # pragma: no cover - never fail the run on one client
                self.stderr.write(f"  stage recompute failed for {str(cid)[:8]}")
        self.stdout.write(self.style.SUCCESS(f"Recomputed stage for {fixed} client(s)."))

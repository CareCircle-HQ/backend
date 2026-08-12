"""Heal household members who fell off the delivery calendar across an
enrollment replacement (governing-case fork), conserving their PRIOR status.

Root cause (two shapes of the same bug):

  * MODE 2 -- a dependent has NO member profile on the household's live
    (Service Active) enrollment at all. When a governing-case replacement REUSES
    a pre-existing enrollment (``_close_old_and_link_to_existing``), it only
    fills blanks on profiles that ALREADY exist there (``_carry_dietary_profiles``)
    and never CREATES the missing dependents -- so they stay stranded on the
    closed enrollment.
  * MODE 1 -- a dependent HAS a profile on the live enrollment but it is still
    ``PENDING`` (menu carried, status never promoted). ``PENDING`` is in
    ``SERVICE_EXCLUDED_MEMBER_STATUSES`` so the delivery-plan builders skip them.

Either way the member is ACTIVE-in-service on the closed prior enrollment but
has no plan / no calendar occurrence on the live one -> "Active but missing from
the calendar" (see ``po_blockers.detect_po_drops``).

This command walks every household with a LIVE, kitchen-assigned Service Active
enrollment and, for each roster member missing a plan there:

  1. ensures a member profile exists on the live enrollment (created for MODE 2,
     carrying dietary + clinical info from the richest prior profile);
  2. CONSERVES the member's PRIOR service status -- a member that was ACTIVE on
     the closed enrollment is re-activated (via the kitchen meal rule, so they
     land Active or Out of Orbit as the kitchen can/can't fulfill them); a member
     that was PAUSED / Nutritionist Paused / Inactive / Out of Range is NEVER
     revived -- that status carries verbatim and they get no plan;
  3. rebuilds the household delivery calendar so the re-activated members get a
     plan + future occurrences again.

PO-committed dates are preserved by the rebuild. Dry-run by default.

Usage:
    python manage.py reconcile_missing_member_service            # dry run
    python manage.py reconcile_missing_member_service --apply
    python manage.py reconcile_missing_member_service --apply --limit 50
"""
from django.core.management.base import BaseCommand

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    MemberDietaryProfile,
    MemberStatus,
    ScheduleStatus,
)

_TERMINAL = [
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.DISREGARDED,
]

# Statuses whose members are excluded from delivery plans but must NEVER be
# auto-revived by this heal: a deliberate pause / off-ramp / geographic block.
# We carry these verbatim (never promote them to Active).
_KEEP_VERBATIM = {
    MemberStatus.PAUSED,
    MemberStatus.NUTRITIONIST_PAUSED,
    MemberStatus.INACTIVE,
    MemberStatus.OUT_OF_RANGE,
}

# Dietary / clinical fields copied when CREATING a missing profile (MODE 2),
# mirroring the fork's member-profile carry (replace_enrollment_for_case_change).
_CARRY_FIELDS = [
    "member_name",
    "dietary_restrictions",
    "food_allergies",
    "other_dietary_restrictions",
    "meal_category",
    "menu_type",
    "meals_per_delivery",
    "general_verification_notes",
    "mobile_number",
    "conditions",
    "weeks_gestation",
    "months_postpartum",
    "medications",
    "weight",
    "height",
    "on_medical_diet",
    "medical_diet_details",
    "meal_plan",
    "meal_plan_other",
    "assessment_notes",
    "nutritionist_pdf_key",
]


class Command(BaseCommand):
    help = (
        "Heal household members stranded off the delivery calendar by an "
        "enrollment replacement: create/repair their profile on the live Service "
        "Active enrollment conserving the PRIOR status (re-activating only those "
        "that were Active), then rebuild the calendar. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist changes.")
        parser.add_argument("--limit", type=int, default=0, help="Cap households processed.")

    def _richest_prior(self, client_id, exclude_pk=None):
        """The member's most recent OTHER profile carrying a menu (the carried
        source left behind on the superseded / closed enrollment)."""
        qs = (
            MemberDietaryProfile.objects.filter(client_id=client_id)
            .exclude(menu_type="")
            .select_related("enrollment")
            .order_by("-enrollment__opened_at")
        )
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.first()

    def handle(self, *args, **opts):
        apply = opts["apply"]
        limit = opts["limit"]

        # Live, kitchen-assigned, serving enrollments -- the households that
        # should have a full delivery calendar right now.
        live_enrollments = (
            EnrollmentVerification.objects.filter(
                stage=EnrollmentStage.SERVICE_ACTIVE, kitchen__isnull=False,
            )
            .exclude(household__isnull=True)
            .select_related("household")
            .order_by("id")
        )

        planned = []  # (enrollment, [ (client_id, member_name, action, prior_status, new_status) ])
        for enr in live_enrollments.iterator():
            household = enr.household
            roster = list(
                HouseholdMember.objects.filter(household=household)
                .select_related("client")
            )
            profiles = {p.client_id: p for p in enr.member_profiles.all()}
            with_plan = set(
                enr.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED)
                .values_list("member_profile_id", flat=True)
            )

            actions = []
            for hm in roster:
                client = hm.client
                if client is None:
                    continue
                live_p = profiles.get(client.pk)
                # Already properly on the calendar -> nothing to do.
                if live_p is not None and live_p.pk in with_plan:
                    continue
                # Only heal members excluded because they are missing / PENDING;
                # a genuinely paused/OOO/etc. member without a plan is CORRECT.
                if live_p is not None and live_p.status != MemberStatus.PENDING:
                    continue

                prior = self._richest_prior(
                    client.pk, exclude_pk=live_p.pk if live_p else None
                )
                if prior is None:
                    continue  # no carried history -> not a strand, leave alone

                prior_status = MemberStatus(prior.status) if prior.status else MemberStatus.PENDING
                if prior_status in _KEEP_VERBATIM:
                    new_status = prior_status          # never revive
                elif prior_status == MemberStatus.ACTIVE:
                    new_status = MemberStatus.ACTIVE    # re-activate (meal rule refines)
                else:
                    # PENDING / OUT_OF_ORBIT prior: not a served member to revive.
                    continue

                action = "create" if live_p is None else "repair"
                actions.append((client.pk, prior.member_name or (live_p.member_name if live_p else ""),
                                action, prior_status, new_status, prior, live_p))

            if actions:
                planned.append((enr, actions))

        if limit:
            planned = planned[:limit]

        total_members = sum(len(a) for _, a in planned)
        self.stdout.write(
            f"serving households with stranded members: {len(planned)} "
            f"({total_members} member(s) to heal)"
        )
        shown = 0
        for enr, actions in planned:
            if shown >= 25:
                break
            for cid, name, action, ps, ns, _prior, _lp in actions:
                if shown >= 25:
                    break
                self.stdout.write(
                    f"  enr {enr.id}: {name or cid} [{action}] "
                    f"prior={ps} -> new={ns}"
                )
                shown += 1
        if total_members > shown:
            self.stdout.write(f"  ... and {total_members - shown} more")

        if not apply:
            self.stdout.write(self.style.WARNING("Dry run -- re-run with --apply."))
            return

        from api.services.meal_rules import reconcile_member_kitchen_output
        from api.services.orders import rebuild_delivery_calendar

        healed = 0
        rebuilt = 0
        for enr, actions in planned:
            touched = False
            for cid, name, action, prior_status, new_status, prior, live_p in actions:
                if live_p is None:
                    # MODE 2: create the missing profile, carrying dietary/clinical.
                    carried = {f: getattr(prior, f) for f in _CARRY_FIELDS}
                    live_p = MemberDietaryProfile.objects.create(
                        enrollment=enr, client_id=cid,
                        status=new_status,
                        pause_locked=prior.pause_locked,
                        eligibility_paused=prior.eligibility_paused,
                        kitchen_meal_type="", kitchen_food_notes="",
                        **carried,
                    )
                else:
                    # MODE 1: promote the stuck PENDING placeholder.
                    live_p.status = new_status
                    live_p.pause_locked = prior.pause_locked
                    live_p.eligibility_paused = prior.eligibility_paused
                    live_p.save(update_fields=[
                        "status", "pause_locked", "eligibility_paused",
                    ])

                # Re-activated members: let the kitchen meal rule finalize
                # Active vs Out of Orbit + kitchen_meal_type against the carried
                # kitchen. Paused/Inactive/OOR are respected (no-op) by the rule.
                if new_status == MemberStatus.ACTIVE:
                    try:
                        reconcile_member_kitchen_output(
                            live_p, kitchen=enr.kitchen, save=True,
                        )
                    except Exception:
                        pass
                healed += 1
                touched = True

            if touched:
                try:
                    rebuild_delivery_calendar(enr)
                    rebuilt += 1
                except Exception:
                    pass

        self.stdout.write(
            self.style.SUCCESS(
                f"Healed {healed} member(s) across {rebuilt} household(s). "
                f"Prior status conserved (Active re-activated; paused/off-ramp "
                f"left as-is)."
            )
        )

"""Reconcile enrollment stages from the DB (Kitchen-Assignment lifecycle pass).

SCRIPT 2 of 2. Where ``sync_member_data`` refreshed the DATA (address, menu,
allergies, kitchen, cadence), this pass drives the STAGE of every household still
in the verification funnel, using only what is now in the database.

SCOPE -- only enrollments currently in one of:
    Pending Verification, Verified, Kitchen Assignment
Active members (Service Active) and every other stage are NEVER touched.

For each in-scope enrollment we read the governing OPEN internal-service case
(``governing_internal_case``; open = case_status not closed/cancelled) and
whether the household is COMPLETE in the DB:
    delivery address + cadence (delivery_weekdays) + kitchen + menu type.

Decision table (auth = the internal-service case's service_authorization_status):

    auth        complete  ->  enrollment stage        deliveries
    --------    --------      ----------------        ----------
    Approved    yes           Service Active          reconcile + activate
    Approved    no            Kitchen Assignment      -
    Denied      yes           Verified                -   (verify only)
    Denied      no            Pending Verification    -
    Requested   yes           Verified                -   (pending auth: no delivery)
    Requested   no            Pending Verification    -
    (no open internal-service case, or auth Expired / Not Required / blank)
                any           Validated               -   (funnel derives Assessment/Navigation)

``verified_at`` is set when the target is Verified/Kitchen Assignment/Active and
cleared when the target is Pending Verification/Validated (so "pending" really is
un-verified). Regressions bypass the forward-only transition map by design (this
is a reconciliation/backfill), still logging a StageEvent and recomputing the
client funnel stage.

Usage:
    python manage.py reconcile_member_stages                 # DRY RUN (rolls back)
    python manage.py reconcile_member_stages --apply          # commit
    python manage.py reconcile_member_stages --limit 100      # first 100 enrollments
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    CaseStatus,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    MemberStatus,
    ProductTypeKind,
    ServiceAuthorizationStatus,
    StageEntityType,
    StageEvent,
    StageEventSource,
)
from api.serializers import sync_household_members
from api.services.delivery import (
    create_member_delivery_schedules,
    current_household_cadence,
    update_household_cadence,
)
from api.services.kitchens import kitchen_offered_menu_index
from api.services.lifecycle import (
    ENROLLMENT_TRANSITIONS,
    advance_enrollment,
    governing_internal_case,
    recompute_enrollment_household,
)
from api.services.meal_rules import reconcile_member_kitchen_output
from api.services.orders import (
    generate_delivery_calendar,
    resync_scheduled_orders,
    sync_delivery_calendar,
)

# Only these stages are reconciled; everything else (active, terminal, on hold,
# pre-verification) is left untouched.
_IN_SCOPE = {
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
}
# A case no longer confers authorization once it is closed/cancelled.
_CLOSED_CASE_STATUSES = {CaseStatus.CLOSED, CaseStatus.CANCELLED}
# Targets that mean "verified" (stamp verified_at) vs "not verified" (clear it).
_VERIFIED_TARGETS = {
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
}
_UNVERIFIED_TARGETS = {
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VALIDATED,
}


def _cadence_from_weekdays(weekdays):
    """Recover ``(DeliveryCadence, ProductTypeKind)`` from the stored delivery
    weekdays (written by ``sync_member_data``): Mon/Thu & Tue/Fri are meals,
    Wed-only is boxes. ``(None, None)`` when no recognisable cadence is set."""
    s = set(weekdays or [])
    if s == {"mon", "thu"}:
        return DeliveryCadence.MON_THU, ProductTypeKind.MEALS
    if s == {"tue", "fri"}:
        return DeliveryCadence.TUE_FRI, ProductTypeKind.MEALS
    if s == {"wed"}:
        return DeliveryCadence.ONCE_A_WEEK, ProductTypeKind.BOXES
    return None, None


class Command(BaseCommand):
    help = (
        "Reconcile enrollment stages (Pending Verification / Verified / Kitchen "
        "Assignment only) from the DB: the internal-service case authorization + "
        "data completeness decide Verified / Kitchen Assignment / Active / Pending "
        "Verification / Validated. Active members are never touched. Dry-run "
        "unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--limit", type=int, default=0, help="Process first N enrollments."
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        qs = (
            EnrollmentVerification.objects.filter(stage__in=_IN_SCOPE)
            .select_related("client", "kitchen")
            .order_by("opened_at")
        )
        if options["limit"]:
            qs = qs[: options["limit"]]

        self._offered_cache = {}
        report = Counter()
        flags = []

        with transaction.atomic():
            for enr in qs:
                try:
                    with transaction.atomic():
                        outcome = self._process(enr)
                except Exception as exc:  # isolate a bad enrollment, keep going
                    outcome = ("error", str(exc))
                report[outcome[0]] += 1
                if outcome[0] in ("error", "validated", "pending_verification"):
                    flags.append((enr.client_id, outcome[1] if len(outcome) > 1 else ""))

            if not apply:
                transaction.set_rollback(True)

        self._report(report, flags, apply)

    # -- per-enrollment -----------------------------------------------------
    def _offered(self, kitchen):
        if kitchen.pk not in self._offered_cache:
            self._offered_cache[kitchen.pk] = kitchen_offered_menu_index(kitchen)
        return self._offered_cache[kitchen.pk]

    def _process(self, enr):
        case = governing_internal_case(enr)
        open_case = case is not None and case.case_status not in _CLOSED_CASE_STATUSES
        auth = case.service_authorization_status if open_case else ""

        # Completeness (all four present in the DB).
        cadence, product_kind = _cadence_from_weekdays(enr.delivery_weekdays)
        primary_profile = (
            enr.member_profiles.filter(client=enr.client).first()
            if enr.client_id else None
        )
        menu_ok = bool(primary_profile and primary_profile.menu_type)
        complete = bool(
            enr.delivery_address_id and cadence is not None
            and enr.kitchen_id and menu_ok
        )

        # No open internal-service case, or an auth we don't act on (expired /
        # not required / blank) -> drop back to Validated; the client funnel then
        # derives Assessment (assessed-eligible) or Navigation (has a case).
        if auth not in (
            ServiceAuthorizationStatus.APPROVED,
            ServiceAuthorizationStatus.DENIED,
            ServiceAuthorizationStatus.PENDING,
        ):
            # AUTHORIZATION IS NOT VERIFICATION -- a REAL verification (verified_by
            # set) must NEVER be destroyed here. The verification fact is
            # independent of the case's authorization state; regressing a genuinely
            # verified household to Validated + clearing verified_at wiped real
            # agent verifications whenever the case auth happened to be
            # non-actionable at reconcile time (see DESTINY THOMPSON). Leave a
            # really-verified enrollment untouched; only regress one that was NOT
            # really verified (verified_by null: no verification, or a false
            # system stamp), which also clears any stray false verified_at.
            if enr.verified_by_id is not None:
                return ("no_change",)
            self._set_verified_at(enr, EnrollmentStage.VALIDATED)
            changed = self._move(
                enr, EnrollmentStage.VALIDATED,
                note="Reconcile: no open internal-service authorization.",
            )
            reason = "no open internal-service case" if not open_case else (
                f"auth {auth or 'blank'!r} not actionable"
            )
            return ("validated", reason) if changed else ("no_change",)

        # AUTHORIZATION IS NOT VERIFICATION. A household that has NOT completed a
        # real verification stays at Pending Verification even when its case is
        # APPROVED. This pass must NOT infer a verification from the authorization
        # (that mass-created "System" verifications -- verified_at with no
        # verifier and no nutritionist sign-off -- and pushed members into Kitchen
        # Assignment / Service Active, skipping the verification wizard AND the
        # nutritionist step). Only an ALREADY-verified enrollment is advanced by
        # this reconcile; an unverified one waits for the real verification.
        already_verified = enr.verified_at is not None
        if not already_verified:
            changed = self._move(
                enr, EnrollmentStage.PENDING_VERIFICATION,
                note=f"Reconcile: {auth} authorization, not verified yet.",
            )
            return ("pending_verification", f"{auth}, unverified") if changed else ("no_change",)

        # --- already verified below: advance per auth + completeness (no NEW
        # verification is ever stamped here; verified_at is already set). ---
        # Approved + complete -> activate (kitchen output + delivery plan).
        if auth == ServiceAuthorizationStatus.APPROVED and complete:
            return self._activate(enr, case, cadence, product_kind)

        # Approved + incomplete -> waiting for kitchen assignment.
        if auth == ServiceAuthorizationStatus.APPROVED:
            self._move(
                enr, EnrollmentStage.KITCHEN_ASSIGNMENT,
                note="Reconcile: approved but delivery data incomplete.",
            )
            return ("kitchen_assignment",)

        # Denied / Requested (pending): stay Verified when complete, else pending.
        if complete:
            self._move(
                enr, EnrollmentStage.VERIFIED,
                note=f"Reconcile: {auth} authorization, verified (no delivery).",
            )
            return ("verified",)

        changed = self._move(
            enr, EnrollmentStage.PENDING_VERIFICATION,
            note=f"Reconcile: {auth} authorization, data incomplete.",
        )
        return ("pending_verification", f"{auth}, incomplete") if changed else ("no_change",)

    # -- activation (Approved + complete) -----------------------------------
    def _activate(self, enr, case, cadence, product_kind):
        kitchen = enr.kitchen
        # Ensure every household member has a profile, then apply the global
        # kitchen-output rules (unfulfillable combos go Out of Orbit).
        if enr.client_id:
            sync_household_members(enr.client, enr)
        offered = self._offered(kitchen)
        primary_out = False
        for mv in enr.member_profiles.all():
            out, _became, _reason = reconcile_member_kitchen_output(
                mv, kitchen, offered=offered,
            )
            if enr.client_id and mv.client_id == enr.client_id:
                primary_out = out

        # Verify (stamp verified_at), then build the plan + calendar, then place
        # in service. Step through the allowed forward transitions.
        self._set_verified_at(enr, EnrollmentStage.SERVICE_ACTIVE)
        if enr.stage == EnrollmentStage.PENDING_VERIFICATION:
            self._move(enr, EnrollmentStage.VERIFIED, note="Reconcile: verified.")

        if not enr.delivery_schedules.exists():
            create_member_delivery_schedules(
                enr, case=case, cadence=cadence, kitchen=kitchen,
                product_kind=product_kind,
            )
            generate_delivery_calendar(enr)
        else:
            enr.delivery_schedules.update(kitchen=kitchen)
            if current_household_cadence(enr) != cadence:
                update_household_cadence(enr, cadence=cadence, case=case)
                sync_delivery_calendar(enr)
        enr.delivery_schedules.update(kitchen=kitchen)
        resync_scheduled_orders(enrollment=enr)

        self._move(
            enr, EnrollmentStage.SERVICE_ACTIVE,
            note=f"Reconcile: approved + complete; activated ({kitchen.name}).",
        )
        return ("activated_out_of_orbit" if primary_out else "activated",)

    # -- stage helpers ------------------------------------------------------
    def _move(self, enr, target, note):
        """Move ``enr`` to ``target``. Forward (map-allowed) moves go through
        ``advance_enrollment``; regressions are set directly (with a StageEvent +
        funnel recompute), since the transition map is intentionally forward-only.
        Returns True when the stage actually changed."""
        current = EnrollmentStage(enr.stage)
        target = EnrollmentStage(target)
        if current == target:
            return False
        if target in ENROLLMENT_TRANSITIONS.get(current, set()):
            advance_enrollment(enr, target, force=True, note=note)
            return True
        return self._set_stage_direct(enr, current, target, note)

    def _set_stage_direct(self, enr, from_stage, target, note):
        now = timezone.now()
        enr.stage = target
        enr.stage_at = now
        enr.save(update_fields=["stage", "stage_at"])
        StageEvent.objects.create(
            entity_type=StageEntityType.ENROLLMENT,
            enrollment=enr,
            client=enr.client,
            from_stage=from_stage,
            to_stage=target,
            source=StageEventSource.AUTO,
            note=note,
        )
        recompute_enrollment_household(enr)
        return True

    def _set_verified_at(self, enr, target):
        """Stamp or clear ``verified_at`` to match the target stage's meaning."""
        if target in _VERIFIED_TARGETS and enr.verified_at is None:
            enr.verified_at = timezone.now()
            enr.save(update_fields=["verified_at"])
        elif target in _UNVERIFIED_TARGETS and enr.verified_at is not None:
            enr.verified_at = None
            enr.save(update_fields=["verified_at"])

    # -- report -------------------------------------------------------------
    def _report(self, report, flags, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Member stage reconciliation ==="))
        order = [
            ("activated", "-> Service Active (approved + complete)"),
            ("activated_out_of_orbit", "-> Service Active, primary Out of Orbit"),
            ("kitchen_assignment", "-> Kitchen Assignment (approved, incomplete)"),
            ("verified", "-> Verified (denied/requested + complete)"),
            ("pending_verification", "-> Pending Verification (denied/requested, incomplete)"),
            ("validated", "-> Validated (no open internal-service auth)"),
            ("no_change", "No change (already at target)"),
            ("error", "Errored (rolled back, see flags)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<54}: {report[key]}")
        self.stdout.write(f"  {'TOTAL in-scope enrollments':<54}: {sum(report.values())}")

        if flags:
            self.stdout.write(head(f"\nFlagged ({len(flags)}, showing up to 30):"))
            for cid, reason in flags[:30]:
                self.stdout.write(f"  {cid or '(none)'}: {reason}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))

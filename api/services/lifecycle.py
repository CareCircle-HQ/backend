"""Client lifecycle + enrollment stage transitions.

Two layers:

* The acquisition funnel (``Client.lifecycle_stage``) is *derived* from synced
  Unite Us data and advanced automatically by :func:`recompute_client_stage`.
* Service delivery (``Enrollment.stage``) is advanced by explicit, guarded
  manual transitions via :func:`advance_enrollment`.

Every transition appends a :class:`~api.models.StageEvent` row, which is the
source of truth for funnel-conversion and time-in-stage reporting.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from api.models import (
    ClientStage,
    EnrollmentStage,
    ProcessResult,
    ProcessStatus,
    ProcessType,
    ScreenStatus,
    ServiceAuthorizationStatus,
    StageEntityType,
    StageEvent,
    StageEventSource,
)

# Met Council provider id (overridable via settings). Used to scope the
# "screened" and "client" funnel checks to the logged-in provider's own work.
MET_COUNCIL_PROVIDER_ID = getattr(
    settings, "MET_COUNCIL_PROVIDER_ID", "12706c81-03a1-4cdb-954a-579929cd05df"
)


class InvalidTransition(ValueError):
    """Raised when an enrollment stage transition is not allowed."""


# ---------------------------------------------------------------------------
# Acquisition funnel (Client.lifecycle_stage)
# ---------------------------------------------------------------------------
def _is_met_council(provider_id, org_name):
    """Best-effort Met Council match: by provider id, else by name. Returns True
    when no provider info is present (data is assumed already provider-scoped)."""
    if provider_id and str(provider_id).lower() == str(MET_COUNCIL_PROVIDER_ID).lower():
        return True
    if org_name and "met council" in org_name.lower():
        return True
    return not provider_id and not org_name


def _has_met_council_screening(client):
    for s in client.screenings.all():
        if s.screen_status != ScreenStatus.COMPLETED:
            continue
        if _is_met_council(s.provider_id, s.performing_organization_name or s.provider_name):
            return True
    return False


def _has_met_council_case(client):
    for c in client.cases.all():
        pid = c.provider_id or c.originating_provider_id
        name = c.provider_name or c.originating_provider_name
        if _is_met_council(pid, name):
            return True
    return False


def _assessment_outcome(client):
    """Return "eligible" / "ineligible" / None from the client's assessments.

    Prefers "eligible" when results are mixed. None when no assessment has a
    resolved eligibility result yet.
    """
    statuses = [
        (e.eligible_status or "").strip().lower()
        for e in client.assessments.all()
        if (e.eligible_status or "").strip()
    ]
    if not statuses:
        return None

    def is_ineligible(s):
        return "ineligible" in s or "not eligible" in s

    if any("eligible" in s and not is_ineligible(s) for s in statuses):
        return "eligible"
    if any(is_ineligible(s) for s in statuses):
        return "ineligible"
    return None


def _derive_early_funnel(client):
    """Early funnel stage from synced Unite Us data (no writes).

    Priority (highest first): navigation > assessment > not_eligible >
    screened > consent > inactive.
    """
    if _has_met_council_case(client):
        return ClientStage.NAVIGATION

    outcome = _assessment_outcome(client)
    if outcome == "eligible":
        return ClientStage.ASSESSMENT
    if outcome == "ineligible":
        return ClientStage.NOT_ELIGIBLE

    if _has_met_council_screening(client):
        return ClientStage.SCREENED

    if client.consent_accepted or (client.consent_status or "").lower() == "accepted":
        return ClientStage.CONSENT

    return ClientStage.INACTIVE


# EnrollmentVerification.stage -> the ClientStage it drives the client to, for
# the stages that actively govern the client (past verification start). DENIED
# is intentionally non-terminal: it parks the client at Waiting Authorization
# ("needs attention", easily re-accepted) rather than an off-ramp.
_ENROLLMENT_DRIVES = {
    EnrollmentStage.PENDING_VERIFICATION: ClientStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED: ClientStage.VERIFIED,
    EnrollmentStage.WAITING_AUTHORIZATION: ClientStage.WAITING_AUTHORIZATION,
    EnrollmentStage.DENIED: ClientStage.WAITING_AUTHORIZATION,
    EnrollmentStage.AUTHORIZED: ClientStage.AUTHORIZED,
    EnrollmentStage.SERVICE_ACTIVE: ClientStage.ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE: ClientStage.COMPLETED,
}

# Rank used to pick the most-advanced enrollment when a client has several.
# Paused/terminal stages rank 0 so an active enrollment always wins.
_ENROLLMENT_RANK = {
    EnrollmentStage.ON_HOLD: 0,
    EnrollmentStage.CLOSED: 0,
    EnrollmentStage.CANCELLED: 0,
    EnrollmentStage.PENDING_VALIDATION: 1,
    EnrollmentStage.VALIDATED: 2,
    EnrollmentStage.PENDING_VERIFICATION: 3,
    EnrollmentStage.VERIFIED: 4,
    EnrollmentStage.WAITING_AUTHORIZATION: 5,
    EnrollmentStage.DENIED: 5,
    EnrollmentStage.AUTHORIZED: 6,
    EnrollmentStage.SERVICE_ACTIVE: 7,
    EnrollmentStage.SERVICE_COMPLETE: 8,
}


def _governing_enrollments(client):
    """Enrollments that govern this client's lifecycle stage.

    A verification applies to the WHOLE household, so a client is governed by:

    * their own enrollments (where they are the primary), and
    * every enrollment of the household they belong to (as a non-primary
      member).

    That lets every household member inherit the household enrollment's stage,
    so they all move together — to Pending Verification when a verification is
    requested, and to Verified / Active when it completes — not just the primary.
    """
    seen = {e.pk: e for e in client.enrollments.all()}

    # Household enrollments: the client participates via their household
    # membership. The household's members are the source of truth, so a member
    # is governed even before a per-member MemberDietaryProfile row exists.
    membership = getattr(client, "household_membership", None)
    if membership is not None:
        for enr in membership.household.enrollment_verifications.all():
            seen.setdefault(enr.pk, enr)

    # Also honor any MemberDietaryProfile linking the client to an enrollment
    # whose household they aren't a (current) member of (defensive: e.g.
    # membership changed after the row was written).
    for mp in client.member_profiles.select_related("enrollment").all():
        enr = mp.enrollment
        if enr is not None:
            seen.setdefault(enr.pk, enr)
    return list(seen.values())


def _primary_enrollment(client):
    """The enrollment that governs the client's stage: the most-advanced one,
    tie-broken by most-recent stage change. None when the client has none."""
    enrollments = _governing_enrollments(client)
    if not enrollments:
        return None

    def sort_key(e):
        return (
            _ENROLLMENT_RANK.get(EnrollmentStage(e.stage), 0),
            e.stage_at or e.opened_at,
        )

    return max(enrollments, key=sort_key)


def derive_client_stage(client):
    """Compute the client's lifecycle stage (no writes).

    The early funnel governs until an EnrollmentVerification exists; from
    Pending Verification onward the enrollment's stage takes precedence.
    """
    early = _derive_early_funnel(client)
    enr = _primary_enrollment(client)
    if enr is None:
        return early

    stage = EnrollmentStage(enr.stage)

    # Enrollment exists but hasn't reached verification yet: early funnel rules.
    if stage in (EnrollmentStage.PENDING_VALIDATION, EnrollmentStage.VALIDATED):
        return early

    # On hold: don't move the client (keep its current stage).
    if stage == EnrollmentStage.ON_HOLD:
        return client.lifecycle_stage or early

    # Closed / cancelled: terminal off-ramp, but never downgrade a client that
    # already reached Active / Completed.
    if stage in (EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED):
        if client.lifecycle_stage in (ClientStage.ACTIVE, ClientStage.COMPLETED):
            return client.lifecycle_stage
        return ClientStage.NOT_ELIGIBLE

    return _ENROLLMENT_DRIVES.get(stage, early)


@transaction.atomic
def recompute_client_stage(client, *, actor=None, save=True):
    """Derive and apply the client's funnel stage. Logs a StageEvent only when
    the stage changes. Returns the (possibly unchanged) stage.
    """
    target = derive_client_stage(client)
    current = client.lifecycle_stage
    if target == current:
        return target

    client.lifecycle_stage = target
    client.lifecycle_stage_at = timezone.now()
    if save:
        client.save(update_fields=["lifecycle_stage", "lifecycle_stage_at"])

    StageEvent.objects.create(
        entity_type=StageEntityType.CLIENT,
        client=client,
        from_stage=current or "",
        to_stage=target,
        source=StageEventSource.AUTO,
        actor=actor,
    )
    return target


def _enrollment_participant_clients(enrollment):
    """The clients governed by ``enrollment``: every member of its household,
    plus anyone linked through a MemberDietaryProfile row (defensive).

    Falls back to the MemberDietaryProfile rows when the enrollment has no
    household (older/individual enrollments).
    """
    clients = {}
    household = enrollment.household
    if household is not None:
        for hm in household.members.select_related("client").all():
            if hm.client_id:
                clients[hm.client_id] = hm.client
    for mp in enrollment.member_profiles.select_related("client").all():
        if mp.client_id:
            clients.setdefault(mp.client_id, mp.client)
    return list(clients.values())


def _recompute_household_members(enrollment, *, actor=None, exclude_client_id=None):
    """Recompute the lifecycle stage of every participant in ``enrollment``
    other than ``exclude_client_id`` (usually the primary, which the caller has
    already handled).

    The whole household is verified/served under one enrollment, so when the
    enrollment advances each participating member must advance with it — e.g.
    all members go to Pending Verification when the verification is requested,
    and all become Active when the household is placed in service, not just the
    primary.
    """
    for client in _enrollment_participant_clients(enrollment):
        if client is None or client.pk == exclude_client_id:
            continue
        recompute_client_stage(client, actor=actor)


def recompute_enrollment_household(enrollment, *, actor=None):
    """Recompute the lifecycle stage for the enrollment's primary client AND
    every household participant, so the whole group tracks the enrollment stage
    together. Safe to call on creation (Pending Verification) and on every
    stage change."""
    if enrollment.client_id:
        recompute_client_stage(enrollment.client, actor=actor)
    _recompute_household_members(
        enrollment, actor=actor, exclude_client_id=enrollment.client_id
    )


# ---------------------------------------------------------------------------
# Service delivery (Enrollment.stage)
# ---------------------------------------------------------------------------
# Allowed transitions. on_hold can resume into any active stage; terminal stages
# have no outgoing transitions.
ENROLLMENT_TRANSITIONS = {
    EnrollmentStage.PENDING_VALIDATION: {
        EnrollmentStage.VALIDATED,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.VALIDATED: {
        EnrollmentStage.PENDING_VERIFICATION,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.PENDING_VERIFICATION: {
        EnrollmentStage.VERIFIED,
        EnrollmentStage.WAITING_AUTHORIZATION,
        EnrollmentStage.DENIED,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.VERIFIED: {
        EnrollmentStage.WAITING_AUTHORIZATION,
        EnrollmentStage.AUTHORIZED,
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.WAITING_AUTHORIZATION: {
        EnrollmentStage.AUTHORIZED,
        EnrollmentStage.DENIED,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.AUTHORIZED: {
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.DENIED: {
        EnrollmentStage.PENDING_VERIFICATION,
        EnrollmentStage.WAITING_AUTHORIZATION,
        EnrollmentStage.CLOSED,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.SERVICE_ACTIVE: {
        EnrollmentStage.SERVICE_COMPLETE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CLOSED,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.SERVICE_COMPLETE: {EnrollmentStage.CLOSED},
    EnrollmentStage.ON_HOLD: {
        EnrollmentStage.PENDING_VALIDATION,
        EnrollmentStage.VALIDATED,
        EnrollmentStage.PENDING_VERIFICATION,
        EnrollmentStage.VERIFIED,
        EnrollmentStage.WAITING_AUTHORIZATION,
        EnrollmentStage.AUTHORIZED,
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.CLOSED: set(),
    EnrollmentStage.CANCELLED: set(),
}

# Stages that require a passing process before they can be entered.
_PROCESS_GATES = {
    EnrollmentStage.VALIDATED: ProcessType.VALIDATION,
    EnrollmentStage.VERIFIED: ProcessType.VERIFICATION,
}

_TERMINAL_STAGES = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED}


def _has_passing_process(enrollment, process_type):
    return enrollment.processes.filter(
        process_type=process_type,
        status=ProcessStatus.COMPLETED,
        result=ProcessResult.PASS,
    ).exists()


@transaction.atomic
def advance_enrollment(enrollment, to_stage, *, actor=None, note="", force=False):
    """Move an enrollment to ``to_stage`` with guard checks. Logs a StageEvent.

    Raises :class:`InvalidTransition` for illegal transitions or unmet process
    gates. Pass ``force=True`` to bypass the gate checks (still validates the
    transition map unless the current stage is terminal).
    """
    to_stage = EnrollmentStage(to_stage)
    from_stage = EnrollmentStage(enrollment.stage)

    if from_stage == to_stage:
        return enrollment

    allowed = ENROLLMENT_TRANSITIONS.get(from_stage, set())
    if to_stage not in allowed:
        raise InvalidTransition(
            f"Cannot move enrollment {enrollment.pk} from '{from_stage}' to '{to_stage}'."
        )

    gate = _PROCESS_GATES.get(to_stage)
    if gate and not force and not _has_passing_process(enrollment, gate):
        raise InvalidTransition(
            f"Enrollment {enrollment.pk} needs a passing '{gate}' process before "
            f"entering '{to_stage}'."
        )

    now = timezone.now()
    enrollment.stage = to_stage
    enrollment.stage_at = now
    update_fields = ["stage", "stage_at"]
    if to_stage in _TERMINAL_STAGES and enrollment.closed_at is None:
        enrollment.closed_at = now
        update_fields.append("closed_at")
    enrollment.save(update_fields=update_fields)

    stage_event = StageEvent.objects.create(
        entity_type=StageEntityType.ENROLLMENT,
        enrollment=enrollment,
        client=enrollment.client,
        from_stage=from_stage,
        to_stage=to_stage,
        source=StageEventSource.MANUAL,
        actor=actor,
        note=note,
    )

    # On entering AUTHORIZED (Accepted), generate the full delivery schedule.
    # Idempotent (no-ops if orders already exist), so re-entering AUTHORIZED
    # (e.g. after ON_HOLD) never double-creates orders.
    if to_stage == EnrollmentStage.AUTHORIZED:
        from api.services.orders import generate_orders_for_enrollment

        generate_orders_for_enrollment(enrollment)

    # Mirror the transition onto the client's central timeline (best-effort:
    # a timeline hiccup must never roll back the stage change).
    try:
        from api.services import timeline

        actor_name = getattr(actor, "get_full_name", lambda: "")() or getattr(
            actor, "username", ""
        )
        timeline.event_for_verification(
            enrollment, stage_event=stage_event, actor=actor_name or ""
        )
    except Exception:  # pragma: no cover - defensive
        import logging

        logging.getLogger(__name__).exception("timeline.event_for_verification failed")

    # Keep the whole household's lifecycle stage in sync with this enrollment:
    # the primary AND every non-denied participant advance together (e.g. all
    # members go Active when placed in service, not just the primary).
    recompute_enrollment_household(enrollment, actor=actor)
    return enrollment


# Case authorization status -> the enrollment stage it should drive the
# enrollment to (only applied once the enrollment is past verification).
_AUTH_STATUS_TO_STAGE = {
    ServiceAuthorizationStatus.APPROVED: EnrollmentStage.AUTHORIZED,
    ServiceAuthorizationStatus.NOT_REQUIRED: EnrollmentStage.AUTHORIZED,
    ServiceAuthorizationStatus.DENIED: EnrollmentStage.DENIED,
    ServiceAuthorizationStatus.EXPIRED: EnrollmentStage.ON_HOLD,
    ServiceAuthorizationStatus.PENDING: EnrollmentStage.WAITING_AUTHORIZATION,
}

# Stages from which an authorization outcome may be applied. Before verification
# is complete we never act on the case's authorization (a case accepted early
# just waits until the household is verified).
_AUTH_ELIGIBLE_STAGES = {
    EnrollmentStage.VERIFIED,
    EnrollmentStage.WAITING_AUTHORIZATION,
}


def reconcile_enrollment_authorization(enrollment, *, actor=None, note=""):
    """Project the linked Case's authorization status onto the enrollment stage.

    This is the single chokepoint for the externally-driven "Accepted" outcome:
    callers (verification completion, the nightly Unite Us import, a manual
    "mark Accepted" action) all funnel through here rather than touching orders
    directly. Order generation happens as a side-effect of entering AUTHORIZED
    inside :func:`advance_enrollment`.

    No-ops unless the enrollment is eligible (past verification) and the case
    has an actionable authorization status. Idempotent.
    """
    case = enrollment.case
    if case is None:
        return enrollment
    if EnrollmentStage(enrollment.stage) not in _AUTH_ELIGIBLE_STAGES:
        return enrollment

    target = _AUTH_STATUS_TO_STAGE.get(case.service_authorization_status)
    if target is None:
        # Blank / unknown status -> we are still waiting on the authority.
        target = EnrollmentStage.WAITING_AUTHORIZATION

    if EnrollmentStage(enrollment.stage) == target:
        return enrollment
    try:
        return advance_enrollment(enrollment, target, actor=actor, note=note)
    except InvalidTransition:
        # Defensive: an illegal projection (e.g. terminal stage) is a no-op.
        return enrollment

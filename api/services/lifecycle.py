"""Client lifecycle + enrollment stage transitions.

Two layers:

* The acquisition funnel (``Client.lifecycle_stage``) is *derived* from synced
  Unite Us data and advanced automatically by :func:`recompute_client_stage`.
* Service delivery (``Enrollment.stage``) is advanced by explicit, guarded
  manual transitions via :func:`advance_enrollment`.

Every transition appends a :class:`~api.models.StageEvent` row, which is the
source of truth for funnel-conversion and time-in-stage reporting.
"""

from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from api.models import (
    CaseType,
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
        # Tolerate both "completed" (enum) and "complete" (Unite Us export /
        # extension list-view label) so a finished screening always counts.
        if not (s.screen_status or "").strip().lower().startswith("complete"):
            continue
        # Screening has no provider_id column; match Met Council by org/provider
        # name (getattr keeps this safe if a provider_id is added later).
        provider_id = getattr(s, "provider_id", None)
        if _is_met_council(provider_id, s.performing_organization_name or s.provider_name):
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


# An internal-service (meals/boxes) case's authorization status -> the
# ClientStage it drives the funnel to BEFORE a household enrollment exists.
# Mirrors the enrollment projection (_AUTH_STATUS_TO_STAGE): denied/expired are
# non-terminal and park at Waiting Authorization ("needs attention").
_CASE_AUTH_TO_STAGE = {
    ServiceAuthorizationStatus.APPROVED: ClientStage.AUTHORIZED,
    ServiceAuthorizationStatus.NOT_REQUIRED: ClientStage.AUTHORIZED,
    ServiceAuthorizationStatus.PENDING: ClientStage.WAITING_AUTHORIZATION,
    ServiceAuthorizationStatus.DENIED: ClientStage.WAITING_AUTHORIZATION,
    ServiceAuthorizationStatus.EXPIRED: ClientStage.WAITING_AUTHORIZATION,
}

# Rank to pick the most-advanced authorization stage across a client's cases.
_EARLY_AUTH_RANK = {
    ClientStage.WAITING_AUTHORIZATION: 1,
    ClientStage.AUTHORIZED: 2,
}


def _case_authorization_stage(client):
    """Highest authorization-derived stage across the client's internal-service
    (meals/boxes) cases, or None when none carry an actionable auth status.

    This lets the acquisition funnel reflect a case's authorization outcome even
    before a household EnrollmentVerification exists (e.g. data imported from the
    Unite Us cases export). Once an enrollment exists it governs instead — see
    ``derive_client_stage`` — so this only fills the pre-enrollment gap.
    """
    best, best_rank = None, 0
    for c in client.cases.all():
        if c.case_type != CaseType.INTERNAL_SERVICE:
            continue
        stage = _CASE_AUTH_TO_STAGE.get(c.service_authorization_status)
        if stage is None:
            continue
        rank = _EARLY_AUTH_RANK.get(stage, 0)
        if rank > best_rank:
            best, best_rank = stage, rank
    return best


def _derive_early_funnel(client):
    """Early funnel stage from synced Unite Us data (no writes).

    Priority (highest first): authorized/waiting (from an internal-service
    case's authorization) > navigation > assessment > not_eligible > screened >
    consent > inactive.
    """
    # An internal-service (meals/boxes) case with an authorization outcome
    # advances the funnel past Navigation even without an enrollment.
    auth_stage = _case_authorization_stage(client)
    if auth_stage is not None:
        return auth_stage

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
    EnrollmentStage.KITCHEN_ASSIGNMENT: ClientStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE: ClientStage.ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE: ClientStage.COMPLETED,
}

# Rank used to pick the most-advanced enrollment when a client has several.
# Terminal stages rank 0 so an active enrollment always wins. ON_HOLD ranks as
# high as SERVICE_ACTIVE: a held household reached service, so it must outrank a
# stale earlier-stage enrollment (e.g. a leftover pending_verification row) and
# not drag the member backwards. Between a genuinely active enrollment and a
# held one, the recency tie-break (stage_at) decides.
_ENROLLMENT_RANK = {
    EnrollmentStage.ON_HOLD: 8,
    EnrollmentStage.CLOSED: 0,
    EnrollmentStage.CANCELLED: 0,
    EnrollmentStage.PENDING_VALIDATION: 1,
    EnrollmentStage.VALIDATED: 2,
    EnrollmentStage.PENDING_VERIFICATION: 3,
    EnrollmentStage.VERIFIED: 4,
    EnrollmentStage.WAITING_AUTHORIZATION: 5,
    EnrollmentStage.DENIED: 5,
    EnrollmentStage.AUTHORIZED: 6,
    EnrollmentStage.KITCHEN_ASSIGNMENT: 7,
    EnrollmentStage.SERVICE_ACTIVE: 8,
    EnrollmentStage.SERVICE_COMPLETE: 9,
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


def _held_from_stage(enrollment):
    """The enrollment stage an On Hold enrollment was paused FROM, read off the
    most recent 'to On Hold' StageEvent. None when no such event exists."""
    ev = (
        StageEvent.objects.filter(
            enrollment=enrollment, to_stage=EnrollmentStage.ON_HOLD
        )
        .order_by("-entered_at")
        .first()
    )
    if ev and ev.from_stage:
        try:
            return EnrollmentStage(ev.from_stage)
        except ValueError:
            return None
    return None


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

    # On hold: the member keeps the stage they reached before the hold (a hold
    # is a temporary pause, not a regression). Derive that from the stage the
    # enrollment was held FROM so a stale lifecycle_stage can't drag it back.
    if stage == EnrollmentStage.ON_HOLD:
        held_from = _held_from_stage(enr)
        if held_from is not None:
            return _ENROLLMENT_DRIVES.get(held_from, client.lifecycle_stage or early)
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
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.SERVICE_ACTIVE,
        # A verified household whose case authorization comes back Denied is
        # projected straight to DENIED (non-terminal: parks the client at
        # Waiting Authorization). Without this the reconcile no-ops and the
        # household is stuck showing "verified".
        EnrollmentStage.DENIED,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.WAITING_AUTHORIZATION: {
        EnrollmentStage.AUTHORIZED,
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.DENIED,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.AUTHORIZED: {
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.KITCHEN_ASSIGNMENT: {
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.DENIED: {
        EnrollmentStage.PENDING_VERIFICATION,
        EnrollmentStage.WAITING_AUTHORIZATION,
        # A denial can be superseded by a newer internal-service case: an
        # approval re-advances to Kitchen Assignment, an expiry parks On Hold.
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.ON_HOLD,
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
        EnrollmentStage.KITCHEN_ASSIGNMENT,
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
    ServiceAuthorizationStatus.APPROVED: EnrollmentStage.KITCHEN_ASSIGNMENT,
    ServiceAuthorizationStatus.NOT_REQUIRED: EnrollmentStage.KITCHEN_ASSIGNMENT,
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
    # DENIED is re-evaluable: a denial can be superseded by a newer
    # internal-service case (re-approval / re-submission), so reconcile must be
    # able to move a denied enrollment forward again when the governing
    # (most-recent) case changes outcome.
    EnrollmentStage.DENIED,
}


# Authorization favorability for choosing the GOVERNING internal-service case
# among several. An approval supersedes a denial regardless of dates: a
# household holding at least one approved meal/box authorization IS authorized,
# even if a parallel program was denied (or denied the same day). Higher wins.
_AUTH_FAVOR_RANK = {
    ServiceAuthorizationStatus.APPROVED: 4,
    ServiceAuthorizationStatus.NOT_REQUIRED: 4,
    ServiceAuthorizationStatus.PENDING: 3,
    ServiceAuthorizationStatus.DENIED: 2,
    ServiceAuthorizationStatus.EXPIRED: 1,
}

# Timezone-aware floor so cases with a missing date sort last (never beat a
# dated case) instead of raising on a None comparison.
_DT_FLOOR = datetime.min.replace(tzinfo=dt_timezone.utc)


def governing_case_key(case):
    """Descending sort key for picking the governing case among several.

    Priority: most favorable authorization first (an approval beats a denial no
    matter the dates), then most recently opened, then most recently updated,
    then case_id -- a stable, environment-independent final tiebreak so the same
    case is chosen everywhere. The previous date-only sort left exact
    ``date_opened`` ties to arbitrary DB row order, which could pick a denied
    case over an approved one for the same household.
    """
    return (
        _AUTH_FAVOR_RANK.get(case.service_authorization_status, 0),
        case.date_opened or _DT_FLOOR,
        case.updated_at or _DT_FLOOR,
        str(case.case_id),
    )


def governing_internal_case(enrollment):
    """The internal-service case whose authorization governs this enrollment.

    A household can accumulate several internal-service cases (parallel meal/box
    programs, or a denial later followed by a re-approval). The governing one is
    chosen by :func:`governing_case_key` -- an approved authorization wins over a
    denied one regardless of dates -- not whichever case happens to sit on
    ``enrollment.case`` (which may be stale/superseded). Falls back to
    ``enrollment.case`` when the client has no internal-service case.
    """
    client = enrollment.client
    if client is not None:
        cases = [
            c for c in client.cases.all()
            if c.case_type == CaseType.INTERNAL_SERVICE
        ]
        if cases:
            return max(cases, key=governing_case_key)
    return enrollment.case


def reconcile_enrollment_authorization(enrollment, *, actor=None, note=""):
    """Project the governing Case's authorization status onto the enrollment stage.

    This is the single chokepoint for the externally-driven "Accepted" outcome:
    callers (verification completion, the nightly Unite Us import, a manual
    "mark Accepted" action) all funnel through here rather than touching orders
    directly. Order generation happens as a side-effect of entering AUTHORIZED
    inside :func:`advance_enrollment`.

    The governing case is the most recent internal-service case on the client
    (see :func:`governing_internal_case`), not necessarily ``enrollment.case`` --
    a superseded denial must not keep a later re-approval from advancing. The
    enrollment's ``case`` FK is repointed at the governing case so downstream
    consumers (order / delivery windows) read the current authorization.

    No-ops unless the enrollment is eligible (past verification) and the case
    has an actionable authorization status. Idempotent.
    """
    if EnrollmentStage(enrollment.stage) not in _AUTH_ELIGIBLE_STAGES:
        return enrollment

    case = governing_internal_case(enrollment)
    if case is None:
        return enrollment
    # Keep enrollment.case pointing at the governing (current) case so the
    # order/delivery authorization window is read from the right case.
    if enrollment.case_id != case.case_id:
        enrollment.case = case
        enrollment.save(update_fields=["case"])

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

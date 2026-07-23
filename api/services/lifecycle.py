"""Client lifecycle + enrollment stage transitions.

Two layers:

* The acquisition funnel (``Client.lifecycle_stage``) is *derived* from synced
  Unite Us data and advanced automatically by :func:`recompute_client_stage`.
* Service delivery (``Enrollment.stage``) is advanced by explicit, guarded
  manual transitions via :func:`advance_enrollment`.

Every transition appends a :class:`~api.models.StageEvent` row, which is the
source of truth for funnel-conversion and time-in-stage reporting.
"""

import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from api.models import (
    CaseStatus,
    CaseType,
    ClientStage,
    EnrollmentStage,
    InsurancePlanType,
    ProcessResult,
    ProcessStatus,
    ProcessType,
    RecordStatus,
    ScreenStatus,
    ServiceAuthorizationStatus,
    SocialCareCoverageStatus,
    StageEntityType,
    StageEvent,
    StageEventSource,
)

# Met Council provider id (overridable via settings). Used to scope the
# "screened" and "client" funnel checks to the logged-in provider's own work.
MET_COUNCIL_PROVIDER_ID = getattr(
    settings, "MET_COUNCIL_PROVIDER_ID", "12706c81-03a1-4cdb-954a-579929cd05df"
)
# Met Council's managing-organization display name (the case "Organization" in
# Unite Us). Overridable via settings.
MET_COUNCIL_PROVIDER_NAME = getattr(
    settings, "MET_COUNCIL_PROVIDER_NAME", "Met Council - SCN - PHS"
)


def is_met_council_case(*, originating_provider_id=None, provider_id=None,
                        provider_name=None, allow_originating=True):
    """Whether a case belongs to Met Council. It is Met Council when it is
    MANAGED/serviced by Met Council (``provider_id`` == the Met Council id, or
    ``provider_name`` == "Met Council - SCN - PHS"), and -- when
    ``allow_originating`` is True -- also when it was merely CREATED by Met
    Council (``originating_provider_id`` == the Met Council id).

    ``allow_originating`` gates the UNION rule. It is True only for
    INTERNAL-SERVICE (meal/box) cases: Met Council originating a meal case keeps
    it in scope even if the managing-provider column is blank on the export. For
    every other case type (Eligibility / Navigation / External) it must be
    False, so a case Met Council merely REFERRED OUT to another org (ECM
    eligibility assessments, etc.) is dropped -- Met Council has to actually
    manage it to be in scope.

    This is the single gate for keeping external-org cases out of the member
    base -- used by every ingestion path (CSV import, nightly Unite Us pull,
    on-demand refresh) and the cleanup command. STRICT: a case with no Met
    Council signal at all is NOT Met Council (so a blank/other-org case is
    dropped). Note the different sources carry different signals -- the CSV
    export has the originating + provider columns, while the live Unite Us API
    only exposes the managing ``provider``."""
    met_id = str(MET_COUNCIL_PROVIDER_ID).strip().lower()
    met_name = MET_COUNCIL_PROVIDER_NAME.strip().casefold()

    def _id(v):
        return str(v).strip().lower() if v not in (None, "") else ""

    if allow_originating and _id(originating_provider_id) == met_id:
        return True
    if _id(provider_id) == met_id:
        return True
    if (provider_name or "").strip().casefold() == met_name:
        return True
    return False


def case_is_met_council(case):
    """Whether a STORED ``Case`` belongs to Met Council, applying the full
    per-case-type rule (the single source of truth for the Cases-tab badge, the
    Remove action, and the cleanup command).

    * INTERNAL-SERVICE (meal/box) cases are Met Council's own programs. They are
      kept when Met Council MANAGES them OR when they carry NO named managing org
      at all (many legitimate meal cases were imported with blank provider
      columns). A meal case explicitly attributed to a DIFFERENT named org (e.g.
      God's Love We Deliver) is NOT Met Council's -- even if Met Council merely
      ORIGINATED (referred) it; the managing org owns it.
    * Every OTHER case type must be MANAGED by Met Council -- originating alone
      (a referral out) does not count.
    """
    from api.models import CaseType

    is_internal = case.case_type == CaseType.INTERNAL_SERVICE
    if is_met_council_case(
        provider_id=case.provider_id,
        provider_name=case.provider_name,
        allow_originating=False,
    ):
        return True
    # An internal-service case with no named managing org is Met Council's.
    if is_internal and not case.provider_id and not (case.provider_name or "").strip():
        return True
    return False


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


def _active_main_category_names():
    """Lowercased names of the ProgramMainCategory rows an admin has marked
    active (Settings > Programs). Empty until at least one is activated."""
    from api.models import ProgramMainCategory

    return {
        (c.name or "").strip().casefold()
        for c in ProgramMainCategory.objects.filter(is_active=True)
        if (c.name or "").strip()
    }


def _is_eligible(client):
    """True when a screening identified a social need under an ACTIVE program we
    serve: any ``Screening.identified_social_needs`` entry whose name matches an
    active :class:`ProgramMainCategory` (screening need labels ARE the category
    names, see api.services.catalog.upsert_main_categories).

    Returns False when no category is active (the default), so the Eligible
    stage only appears once admins opt programs in.
    """
    active = _active_main_category_names()
    if not active:
        return False
    for s in client.screenings.all():
        for need in (s.identified_social_needs or []):
            name = need if isinstance(need, str) else (
                (need or {}).get("name") if isinstance(need, dict) else ""
            )
            if (name or "").strip().casefold() in active:
                return True
    return False


def _derive_early_funnel(client):
    """Early funnel stage from synced Unite Us data (no writes).

    Priority (highest first): eligible > navigation > assessment > not_eligible >
    screened > consent > inactive.

    A case's authorization status NEVER advances the funnel here: authorization
    is a separate dimension on the Case (it gates kitchen assignment for an
    already-verified household), not a funnel stage. A client with an
    internal-service case but no completed verification stays at Navigation.
    """
    # Eligible ranks above Navigation: a screening need under an active program
    # we serve is the strongest early-funnel signal (drives Urgent Care).
    if _is_eligible(client):
        return ClientStage.ELIGIBLE

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
# the stages that actively govern the client (past verification start). A
# verified household whose authorization is pending/denied stays at Verified
# (the authorization outcome is shown separately, sourced from the Case); only
# an approved authorization advances the enrollment to Kitchen Assignment.
_ENROLLMENT_DRIVES = {
    EnrollmentStage.PENDING_VERIFICATION: ClientStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED: ClientStage.VERIFIED,
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
    # Disregarded rows are excluded from governance entirely (see
    # _governing_enrollments); ranked 0 as a defensive fallback.
    EnrollmentStage.DISREGARDED: 0,
    EnrollmentStage.PENDING_VALIDATION: 1,
    EnrollmentStage.VALIDATED: 2,
    EnrollmentStage.PENDING_VERIFICATION: 3,
    EnrollmentStage.VERIFIED: 4,
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
    # A DISREGARDED enrollment is a dismissed verification request kept only for
    # history: it must never govern the client's stage, so the member reverts to
    # their pre-verification funnel stage and leaves the Verification page.
    return [e for e in seen.values() if e.stage != EnrollmentStage.DISREGARDED]


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


def verification_completed(client):
    """True when the household's verification POP-UP was completed for a
    governing enrollment -- the job that captures food allergies, delivery
    address, the Step-4 validation checks, etc.

    Keyed off the explicit ``verified_at`` fact (set only by the pop-up or a
    one-off backfill), never the enrollment stage or the client's
    lifecycle_stage. The authorization outcome (approved/pending/denied) is a
    separate dimension and does not affect this.
    """
    return any(
        e.verified_at is not None for e in _governing_enrollments(client)
    )


def governing_pending_enrollment(client):
    """A governing enrollment sitting at ``PENDING_VERIFICATION`` (and not yet
    verified), else None.

    Scans ALL of the client's governing enrollments -- their own, their
    household's, and any linked via a MemberDietaryProfile -- rather than only
    the single most-advanced one. A member may now hold several enrollments, so
    the pending request that puts them on the Verification page is not
    necessarily the highest-ranked (``_primary_enrollment``) row. A CRM action
    -- e.g. disregarding a request -- must target whichever governing enrollment
    is actually pending. When more than one qualifies, the most recently
    requested/updated wins.
    """
    candidates = [
        e
        for e in _governing_enrollments(client)
        if e.verified_at is None
        and EnrollmentStage(e.stage) == EnrollmentStage.PENDING_VERIFICATION
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.stage_at or e.opened_at)


def derive_client_stage(client, *, ignore_sticky=False):
    """Compute the client's lifecycle stage (no writes).

    The early funnel governs until an EnrollmentVerification exists; from
    Pending Verification onward the enrollment's stage takes precedence.

    Import-time eligibility off-ramps (INELIGIBLE / SERVICE_INACTIVE) are set
    explicitly by ``api.services.eligibility.reconcile_client_eligibility`` and
    are STICKY here: an unrelated recompute (e.g. an enrollment stage change on a
    household member) must not clobber them. Only reconcile_client_eligibility
    clears them, by re-deriving with ``ignore_sticky=True`` once the data
    recovers.
    """
    if not ignore_sticky and client.lifecycle_stage in (
        ClientStage.INELIGIBLE, ClientStage.SERVICE_INACTIVE,
    ):
        return client.lifecycle_stage
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

    # Cancelled: hard off-ramp. A cancellation ends service outright, so it
    # ALWAYS moves the client to Not Eligible -- even one who had reached
    # Active / Completed (used to mark off-boarded / inactive members).
    if stage == EnrollmentStage.CANCELLED:
        return ClientStage.NOT_ELIGIBLE

    # Closed: terminal off-ramp, but never downgrade a client that already
    # reached Active / Completed (service ran its course).
    if stage == EnrollmentStage.CLOSED:
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
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
        # An agent can disregard (dismiss) a pending verification request.
        EnrollmentStage.DISREGARDED,
    },
    EnrollmentStage.VERIFIED: {
        # An approved authorization advances a verified household to Kitchen
        # Assignment (reconcile_enrollment_authorization). A pending/denied/
        # expired authorization leaves it at VERIFIED -- the outcome is shown
        # separately (from the Case), never as a stage. There is NO direct
        # Verified -> Service Active: kitchen assignment is mandatory before a
        # program can become active.
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.KITCHEN_ASSIGNMENT: {
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CANCELLED,
        # De-authorization pull-back: when the governing internal-service
        # authorization is no longer approved (reverted to pending), the
        # reconcile moves the enrollment BACK to Verified so it correctly reads
        # "Waiting Authorization" (see _downgrade_unauthorized_enrollment).
        EnrollmentStage.VERIFIED,
        # An enrollment that reached kitchen assignment but has NO internal-service
        # (meal/box) case backing it is an orphan (its case was deleted/never
        # existed) -- it can be disregarded (dismissed, reversible) rather than
        # cancelled. Not reachable from the portal disregard action, which only
        # targets pending requests (governing_pending_enrollment).
        EnrollmentStage.DISREGARDED,
    },
    EnrollmentStage.SERVICE_ACTIVE: {
        EnrollmentStage.SERVICE_COMPLETE,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CLOSED,
        EnrollmentStage.CANCELLED,
        # De-authorization pull-back (see above): an active member whose
        # governing authorization is no longer approved returns to Verified /
        # Waiting Authorization and stops receiving deliveries.
        EnrollmentStage.VERIFIED,
    },
    EnrollmentStage.SERVICE_COMPLETE: {EnrollmentStage.CLOSED},
    EnrollmentStage.ON_HOLD: {
        EnrollmentStage.PENDING_VALIDATION,
        EnrollmentStage.VALIDATED,
        EnrollmentStage.PENDING_VERIFICATION,
        EnrollmentStage.VERIFIED,
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.CLOSED: set(),
    EnrollmentStage.CANCELLED: set(),
    # Disregarded is non-terminal: a re-request moves it back to pending
    # verification (the ext normally creates a fresh enrollment instead).
    EnrollmentStage.DISREGARDED: {
        EnrollmentStage.PENDING_VERIFICATION,
    },
}

# Stages that require a passing process before they can be entered.
_PROCESS_GATES = {
    EnrollmentStage.VALIDATED: ProcessType.VALIDATION,
    EnrollmentStage.VERIFIED: ProcessType.VERIFICATION,
}

_TERMINAL_STAGES = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED}

# Reaching any of these means the household's verification is complete, so the
# "new client needs verification attention" flag (Client.is_new) is cleared.
_VERIFIED_OR_BEYOND = {
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
}


def _has_passing_process(enrollment, process_type):
    return enrollment.processes.filter(
        process_type=process_type,
        status=ProcessStatus.COMPLETED,
        result=ProcessResult.PASS,
    ).exists()


def clear_new_flag_on_verification_request(enrollment):
    """Requesting (or re-requesting) a verification means the household is now
    being handled, so drop its primary off the Urgent Care ("Need Attention")
    list by clearing ``Client.is_new``. Complements the completion-time clear in
    ``advance_enrollment`` (VERIFIED+). Idempotent + best-effort: a no-op when
    already clear, and a hiccup never breaks the request."""
    client = getattr(enrollment, "client", None)
    if client is not None and client.is_new:
        client.is_new = False
        client.save(update_fields=["is_new"])


# ---------------------------------------------------------------------------
# Urgent Care ("Need Attention") eligibility
#
# A brand-new client is surfaced on the Urgent Care list -- and offered the
# "Request Verification" action -- only when ALL of these hold: an OPEN
# internal-service (meal/box) case, NO verification requested yet, a VALID
# Medicaid insurance, and a VALID social care coverage. This single gate is the
# source of truth for the ``is_new`` flag: the import sets it (see
# api.serializers.CaseSerializer + the daily Unite Us pull) and the
# ``review_urgent_care_candidates`` command backfills anyone missed.
# ---------------------------------------------------------------------------

# Insurance plan types that count as "Medicaid" for the verification gate
# (straight Medicaid or a Dual Medicare/Medicaid plan).
MEDICAID_PLAN_TYPES = frozenset({InsurancePlanType.MEDICAID, InsurancePlanType.DUAL})


def has_valid_medicaid(client):
    """True when the client has at least one ACTIVE Medicaid (or Dual
    Medicare/Medicaid) insurance on file. Expired/inactive/pending don't count,
    and a non-Medicaid active plan (e.g. commercial) doesn't satisfy this."""
    return any(
        i.plan_type in MEDICAID_PLAN_TYPES and i.status == RecordStatus.ACTIVE
        for i in client.insurances.all()
    )


def has_valid_social_care(client):
    """True when the client has at least one ENROLLED social care coverage
    (expired / non-enrolled records don't count)."""
    return any(
        c.status == SocialCareCoverageStatus.ENROLLED
        for c in client.social_care_coverages.all()
    )


def has_open_internal_service_case(client):
    """True when the client holds an internal-service (meal/box) case that is
    not closed/cancelled -- the case the verification + delivery attach to."""
    return any(
        c.case_type == CaseType.INTERNAL_SERVICE
        and c.case_status not in (CaseStatus.CLOSED, CaseStatus.CANCELLED)
        for c in client.cases.all()
    )


def has_verification_request(client):
    """True when a verification has already been requested -- an enrollment
    exists on the client OR on their household. Mirrors the Urgent Care
    (need_attention) exclusion in views_members: the mere existence of an
    enrollment (any stage) means verification is already requested/handled."""
    if client.enrollments.exists():
        return True
    membership = getattr(client, "household_membership", None)
    if membership is not None and membership.household.enrollment_verifications.exists():
        return True
    return False


def is_urgent_care_candidate(client):
    """Whether ``client`` meets every condition to be flagged is_new and offered
    the "Request Verification" action: an open internal-service case, no
    verification requested yet (which also excludes anyone already verified),
    and valid Medicaid + valid social care coverage. Single gate shared by the
    import, the Request Verification endpoint, and the backfill command.

    Once at least one program category is opted in (Settings > Programs), the
    member must ALSO be Eligible -- a screening need under an active program --
    to surface. Before any program is activated this extra check is a no-op, so
    the existing Urgent Care behavior is preserved until programs are turned on.
    """
    base = (
        has_open_internal_service_case(client)
        and not has_verification_request(client)
        and has_valid_medicaid(client)
        and has_valid_social_care(client)
    )
    if not base:
        return False
    if _active_main_category_names() and not _is_eligible(client):
        return False
    return True


def evaluate_is_new_flag(client):
    """Set ``Client.is_new`` when the client is an Urgent Care candidate.

    SET-ONLY: never clears the flag (is_new is cleared elsewhere -- when a
    verification is requested via ``clear_new_flag_on_verification_request`` or
    completed via ``advance_enrollment``). Returns True when it flipped the flag
    on. Idempotent + safe to call repeatedly from the import + the backfill
    command."""
    if not client.is_new and is_urgent_care_candidate(client):
        client.is_new = True
        client.save(update_fields=["is_new"])
        return True
    return False


@transaction.atomic
def advance_enrollment(enrollment, to_stage, *, actor=None, actor_label="", note="", force=False):
    """Move an enrollment to ``to_stage`` with guard checks. Logs a StageEvent.

    Raises :class:`InvalidTransition` for illegal transitions or unmet process
    gates. Pass ``force=True`` to bypass the gate checks (still validates the
    transition map unless the current stage is terminal).

    ``actor`` is the acting ``User`` (recorded on ``StageEvent.actor``).
    ``actor_label`` is a free-form display string for callers whose actor isn't
    a User (e.g. the support portal, where the actor is an ``Agent``): it drives
    the timeline event's ``actor`` and is stored on ``StageEvent.metadata`` for
    audit, so the history shows WHO advanced the enrollment.
    """
    to_stage = EnrollmentStage(to_stage)
    from_stage = EnrollmentStage(enrollment.stage)

    if from_stage == to_stage:
        return enrollment

    # Terminal stages (Closed / Cancelled) have no outgoing transitions in the
    # map, so leaving one (e.g. reactivating a cancelled household) is only
    # possible with force -- a deliberate correction/reinstatement.
    leaving_terminal = force and from_stage in _TERMINAL_STAGES
    allowed = ENROLLMENT_TRANSITIONS.get(from_stage, set())
    if to_stage not in allowed and not leaving_terminal:
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
    elif to_stage not in _TERMINAL_STAGES and enrollment.closed_at is not None:
        # Re-opening a previously terminal enrollment: clear the closure stamp so
        # it counts as active again (drives active_enrollment / PO inclusion).
        enrollment.closed_at = None
        update_fields.append("closed_at")
    enrollment.save(update_fields=update_fields)

    # Verification complete -> clear the "new client needs attention" flag. The
    # flag was set when the client's first internal-service case was created
    # (see CaseSerializer); reaching VERIFIED (or beyond) means the verification
    # it was tracking is done, so drop them off the "Need Attention" list.
    if to_stage in _VERIFIED_OR_BEYOND:
        client = enrollment.client
        if client is not None and client.is_new:
            client.is_new = False
            client.save(update_fields=["is_new"])

    stage_event = StageEvent.objects.create(
        entity_type=StageEntityType.ENROLLMENT,
        enrollment=enrollment,
        client=enrollment.client,
        from_stage=from_stage,
        to_stage=to_stage,
        source=StageEventSource.MANUAL,
        actor=actor,
        note=note,
        metadata={"actor_label": actor_label} if actor_label else {},
    )

    # Delivery orders are NOT generated here. They are built at the manual
    # kitchen-assignment step (MemberAssignKitchenView -> create_member_delivery_
    # schedules + generate_delivery_calendar), which is the only place a kitchen,
    # cadence and delivery weekdays exist. Entering KITCHEN_ASSIGNMENT only marks
    # the household as awaiting that manual step.

    # Mirror the transition onto the client's central timeline (best-effort:
    # a timeline hiccup must never roll back the stage change).
    try:
        from api.services import timeline

        actor_name = actor_label or (
            getattr(actor, "get_full_name", lambda: "")()
            or getattr(actor, "username", "")
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


# Case authorization status -> the enrollment stage it advances to. ONLY an
# approval moves the stage (verified -> kitchen_assignment). Pending / denied /
# expired make NO stage change: authorization is a separate dimension shown from
# the Case, never an enrollment stage. A denial that is later superseded by a
# re-approval advances the still-VERIFIED enrollment when reconcile re-runs.
_AUTH_STATUS_TO_STAGE = {
    ServiceAuthorizationStatus.APPROVED: EnrollmentStage.KITCHEN_ASSIGNMENT,
    ServiceAuthorizationStatus.NOT_REQUIRED: EnrollmentStage.KITCHEN_ASSIGNMENT,
}

# Stages from which an approval may advance the enrollment to kitchen assignment.
# Before verification is complete we never act on the case's authorization (a
# case accepted early just waits until the household is verified).
_AUTH_ELIGIBLE_STAGES = {
    EnrollmentStage.VERIFIED,
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

# A closed/cancelled case no longer confers authorization for FUTURE deliveries.
_CLOSED_CASE_STATUSES = {CaseStatus.CLOSED, CaseStatus.CANCELLED}


def _case_open_rank(case):
    """1 for an open case, 0 for a closed/cancelled one. Used as a governance
    tiebreak so an OPEN approved case outranks a CLOSED approved one."""
    return 0 if case.case_status in _CLOSED_CASE_STATUSES else 1


def governing_case_key(case):
    """Descending sort key for picking the governing case among several.

    Priority: most favorable authorization first (an approval beats a denial no
    matter the dates), then OPEN over closed/cancelled (so a lingering
    superseded case can't keep governing a switch), then most recently opened,
    then most recently updated, then case_id -- a stable,
    environment-independent final tiebreak so the same case is chosen
    everywhere. The previous date-only sort left exact ``date_opened`` ties to
    arbitrary DB row order, which could pick a denied case over an approved one
    for the same household.

    The open-ness rank sits BELOW authorization favor, so an approved case
    (open or closed) still beats a pending one -- during a meals->boxes switch
    the still-approved meals case keeps governing until the boxes case is
    approved, and among two approved cases the open (newer) one wins.
    """
    return (
        _AUTH_FAVOR_RANK.get(case.service_authorization_status, 0),
        _case_open_rank(case),
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


# Enrollment stages that are "pre-verification" for the per-program status.
_PRE_VERIFICATION_STAGES = frozenset({
    EnrollmentStage.PENDING_VALIDATION,
    EnrollmentStage.VALIDATED,
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.DISREGARDED,
})

# Post-verification stages whose authorization window, once past its end date,
# means the program is terminally Authorization Expired.
_AUTH_WINDOW_STAGES = frozenset({
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
})


def program_status(enrollment):
    """Display-only per-program status (see :class:`~api.models.ProgramStatus`).

    Folds the enrollment stage together with its GOVERNING internal-service
    case's authorization + approval window into one linear per-program timeline:

        Pending Verification -> Verified -> Waiting Authorization / Denied ->
        Authorized -> Kitchen Assignment -> Active
        (terminal: On Hold*, Authorization Expired, Closed)

    Never stored -- recomputed on read. ``*`` On Hold is shown while the
    enrollment is paused, regardless of the underlying authorization.
    """
    from api.models import ProgramStatus

    stage = EnrollmentStage(enrollment.stage)

    # A paused program shows On Hold above everything else.
    if stage == EnrollmentStage.ON_HOLD:
        return ProgramStatus.ON_HOLD

    gov = governing_internal_case(enrollment)
    auth = getattr(gov, "service_authorization_status", "") if gov else ""
    case_closed = bool(gov and gov.case_status in _CLOSED_CASE_STATUSES)
    end = getattr(gov, "service_authorization_approval_ends_at", None) if gov else None
    window_expired = bool(end and end.date() < timezone.localdate())
    auth_expired = auth == ServiceAuthorizationStatus.EXPIRED or window_expired

    # Terminal displays.
    if stage in (EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED) or case_closed:
        return ProgramStatus.CLOSED
    if auth_expired and stage in _AUTH_WINDOW_STAGES:
        return ProgramStatus.AUTHORIZATION_EXPIRED

    if stage in _PRE_VERIFICATION_STAGES:
        return ProgramStatus.PENDING_VERIFICATION
    if stage == EnrollmentStage.SERVICE_COMPLETE:
        return ProgramStatus.CLOSED
    if stage == EnrollmentStage.SERVICE_ACTIVE:
        return ProgramStatus.ACTIVE
    if stage == EnrollmentStage.KITCHEN_ASSIGNMENT:
        # Authorized until a kitchen is actually assigned; then Kitchen Assignment.
        return (
            ProgramStatus.KITCHEN_ASSIGNMENT
            if enrollment.kitchen_id
            else ProgramStatus.AUTHORIZED
        )
    if stage == EnrollmentStage.VERIFIED:
        if auth in (
            ServiceAuthorizationStatus.APPROVED,
            ServiceAuthorizationStatus.NOT_REQUIRED,
        ):
            return ProgramStatus.AUTHORIZED
        if auth == ServiceAuthorizationStatus.DENIED:
            return ProgramStatus.DENIED
        if auth == ServiceAuthorizationStatus.PENDING:
            return ProgramStatus.WAITING_AUTHORIZATION
        return ProgramStatus.VERIFIED

    return ProgramStatus.PENDING_VERIFICATION


# Enrollment stages that roll a member up to the "Enrolled" main stage.
_ENROLLED_LEVEL_STAGES = frozenset({
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.ON_HOLD,
})


def main_stage(client):
    """The member-profile "main stage" (display grouping):

        Consent -> Screening -> Assessment -> Navigation -> Eligible ->
        Enrolled   (terminal: Cancelled)

    Rolls the granular ``Client.lifecycle_stage`` + the client's enrollments up
    into the seven headline stages. Enrolled whenever the client holds a live
    enrollment (pending_verification..completed / on_hold); Cancelled only when
    every enrollment is cancelled. Falls back to the stored early-funnel stage.
    Never stored.
    """
    enrollments = _governing_enrollments(client)
    if enrollments:
        if any(
            EnrollmentStage(e.stage) in _ENROLLED_LEVEL_STAGES for e in enrollments
        ):
            return ClientStage.ENROLLED
        if all(
            EnrollmentStage(e.stage) == EnrollmentStage.CANCELLED for e in enrollments
        ):
            return ClientStage.CANCELLED

    stage = client.lifecycle_stage
    # Map any residual enrollment-driven stored value up to Enrolled.
    if stage in (
        ClientStage.PENDING_VERIFICATION,
        ClientStage.VERIFIED,
        ClientStage.KITCHEN_ASSIGNMENT,
        ClientStage.ACTIVE,
        ClientStage.COMPLETED,
    ):
        return ClientStage.ENROLLED
    return stage or ClientStage.INACTIVE


def reconcile_enrollment_authorization(enrollment, *, actor=None, actor_label="", note=""):
    """Project the governing Case's authorization status onto the enrollment stage.

    This is the single chokepoint for the externally-driven "Accepted" outcome:
    callers (verification completion, the nightly Unite Us import, a manual
    "mark Accepted" action) all funnel through here. ONLY an approval advances
    the stage (verified -> kitchen_assignment); pending/denied/expired make no
    stage change (the household stays Verified and the authorization status is
    shown separately).

    The governing case is the most recent internal-service case on the client
    (see :func:`governing_internal_case`), not necessarily ``enrollment.case`` --
    a superseded denial must not keep a later re-approval from advancing. The
    enrollment's ``case`` FK is repointed at the governing case so downstream
    consumers (order / delivery windows) read the current authorization.

    No-ops unless the enrollment is verified and the case is approved. Idempotent.
    """
    if EnrollmentStage(enrollment.stage) not in _AUTH_ELIGIBLE_STAGES:
        return enrollment

    case = governing_internal_case(enrollment)
    if case is None:
        return enrollment
    # Keep enrollment.case pointing at the governing (current) case so the
    # order/delivery authorization window is read from the right case -- but ONLY
    # when that case isn't already claimed by another LIVE enrollment. The
    # per-case unique constraint (uniq_enrollment_verification_per_case) forbids
    # two live enrollments sharing a case, so stealing it would raise
    # IntegrityError and abort the whole reconcile. When it's taken we leave the
    # FK as-is and still read the authorization outcome from `case` below, so the
    # stage still advances.
    if enrollment.case_id != case.case_id:
        taken = (
            case.enrollments.exclude(pk=enrollment.pk)
            .exclude(
                stage__in=(EnrollmentStage.DISREGARDED, EnrollmentStage.CANCELLED)
            )
            .exists()
        )
        if not taken:
            enrollment.case = case
            enrollment.save(update_fields=["case"])

    target = _AUTH_STATUS_TO_STAGE.get(case.service_authorization_status)
    if target is None:
        # Not approved (pending / denied / expired / blank): no stage change.
        # The household stays Verified; the status is surfaced from the Case.
        return enrollment

    if EnrollmentStage(enrollment.stage) == target:
        return enrollment
    try:
        return advance_enrollment(
            enrollment, target, actor=actor, actor_label=actor_label, note=note
        )
    except InvalidTransition:
        # Defensive: an illegal projection (e.g. terminal stage) is a no-op.
        return enrollment


# ---------------------------------------------------------------------------
# Internal-service authorization full-stop (single denied meal/box case)
# ---------------------------------------------------------------------------
# Post-verification enrollment stages a denial pauses. Before verification there
# is nothing to pause; ON_HOLD / terminal stages are left as-is.
_DENIAL_PAUSE_STAGES = {
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
}

# Stamped on the auto-pause StageEvent so the reverse (auto-resume on a later
# favorable authorization) only ever un-pauses enrollments THIS rule paused --
# never a manual Place-on-Hold.
_DENIAL_HOLD_NOTE = "Auto-paused: sole internal-service meal/box case denied."

# Authorization statuses that mean "not yet approved -- awaiting a decision".
# A governing case in one of these states must NOT keep a household in service:
# only an approval (or Not Required) authorizes delivery. Because
# ``governing_case_key`` ranks an approval ABOVE a pending/never-requested case,
# a governing status in this set already means the client has NO approved
# internal-service authorization anywhere. EXPIRED is deliberately excluded --
# that is a post-approval terminal state handled by the delivery-window logic.
_WAITING_AUTH_STATUSES = {
    ServiceAuthorizationStatus.PENDING,
    ServiceAuthorizationStatus.NEVER_REQUESTED,
    "",
}

# Post-verification stages that a not-yet-approved authorization pulls BACK to
# Verified (which then displays "Waiting Authorization"). ON_HOLD is left to the
# denial/close rules; terminal stages are never touched.
_UNAUTH_PULLBACK_STAGES = {
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
}

_WAITING_AUTH_NOTE = (
    "Auto-downgraded: internal-service authorization is not approved yet "
    "(pending) -- returned to Waiting Authorization and future deliveries stopped."
)


def _downgrade_unauthorized_enrollment(enrollment, *, actor=None, actor_label=""):
    """Pull an enrollment that was advanced past Verified BACK to Verified when
    its governing internal-service authorization isn't approved yet, and stop
    future deliveries.

    This is the reverse of an activation: an approval advances Verified ->
    Kitchen Assignment -> Active; if the governing authorization later reads
    pending (e.g. a re-import, or a household activated before its authorization
    landed), the household must not keep being served. The enrollment returns to
    Verified (shown as "Waiting Authorization") and its future delivery
    occurrences are truncated so it drops off Purchase Orders. Auto-resumes via
    ``reconcile_enrollment_authorization`` once the case is approved. Idempotent:
    a no-op for an enrollment already at/behind Verified.
    """
    from api.services.orders import truncate_future_deliveries

    if EnrollmentStage(enrollment.stage) not in _UNAUTH_PULLBACK_STAGES:
        return enrollment
    try:
        advance_enrollment(
            enrollment, EnrollmentStage.VERIFIED, actor=actor,
            actor_label=actor_label, note=_WAITING_AUTH_NOTE, force=True,
        )
    except InvalidTransition:
        return enrollment
    try:
        truncate_future_deliveries(enrollment)
    except Exception:  # pragma: no cover - defensive
        pass
    return enrollment

# Stamped on the pause / cancel StageEvents raised by the case-CLOSURE full stop
# (distinct from the denial note above so the two rules stay independently
# auditable).
_CLOSURE_HOLD_NOTE = "Auto-paused: last open internal-service meal/box case closed."
_CLOSURE_CANCEL_NOTE = "Auto-cancelled: no open internal-service meal/box case remains."


def _actor_name(actor):
    """Best-effort display name for an acting User/Agent (falls back to System)."""
    if actor is None:
        return "System"
    return (
        getattr(actor, "get_full_name", lambda: "")()
        or getattr(actor, "name", "")
        or getattr(actor, "username", "")
        or "System"
    )


def _household_primary(client):
    """The primary member of the client's household, else the client itself."""
    membership = getattr(client, "household_membership", None)
    if membership is not None:
        hm = (
            membership.household.members.filter(is_primary=True)
            .select_related("client")
            .first()
        )
        if hm is not None and hm.client_id:
            return hm.client
    return client


def _write_primary_system_note(client, body, *, author_name="System"):
    """Append a deduped SYSTEM note to the household primary (best-effort). The
    content_hash guard means re-running with the SAME body never duplicates."""
    import hashlib

    from api.models import Note, NoteSource

    primary = _household_primary(client)
    if primary is None:
        return
    chash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if Note.objects.filter(
        client=primary, source=NoteSource.SYSTEM, content_hash=chash
    ).exists():
        return
    Note.objects.create(
        client=primary,
        source=NoteSource.SYSTEM,
        author_name=author_name or "System",
        body=body,
        content_hash=chash,
    )


def _full_stop_close_out(client, governing, *, actor=None, actor_label=""):
    """Pause-then-cancel every actionable governing enrollment when the client's
    LAST open internal-service case has closed.

    Steps (mirrors the product spec): truncate future deliveries + pause the
    household (On Hold) + note the primary, THEN cancel the enrollment(s) + a
    second note. Idempotent: once every governing enrollment is terminal there is
    nothing ``actionable`` left, so a re-import is a no-op (no duplicate
    notes/events). Opens NO tickets -- visibility comes from StageEvents, the
    timeline, and the primary notes.
    """
    from api.services.orders import truncate_future_deliveries

    result = {"paused": False, "cancelled": False}
    govs = _governing_enrollments(client)
    actionable = [
        e
        for e in govs
        if EnrollmentStage(e.stage) not in _TERMINAL_STAGES
        and EnrollmentStage(e.stage) != EnrollmentStage.SERVICE_COMPLETE
    ]
    if not actionable:
        return result

    author = actor_label or _actor_name(actor)
    today = timezone.localdate().isoformat()
    closed_at = getattr(governing, "case_closed_at", None) or getattr(
        governing, "updated_at", None
    )
    closed_on = closed_at.date().isoformat() if closed_at else "an unknown date"
    label = getattr(governing, "program_name", "") or "meal/box"

    # 1) Truncate future deliveries (before pausing, so no ON_HOLD skip) + pause.
    for enr in actionable:
        try:
            truncate_future_deliveries(enr)
        except Exception:  # pragma: no cover - defensive
            pass
        if EnrollmentStage(enr.stage) in _DENIAL_PAUSE_STAGES:
            try:
                advance_enrollment(
                    enr,
                    EnrollmentStage.ON_HOLD,
                    actor=actor,
                    actor_label=actor_label,
                    note=_CLOSURE_HOLD_NOTE,
                )
                result["paused"] = True
            except InvalidTransition:
                pass

    _write_primary_system_note(
        client,
        (
            f"Service paused on {today}: the member's last open internal-service "
            f"case ({governing.case_id} - {label}) closed on {closed_on}. Future "
            f"deliveries were stopped and the household was placed On Hold."
        ),
        author_name=author,
    )

    # 2) Cancel the household (hard off-ramp). SERVICE_COMPLETE / terminal rows
    #    are left as-is (force=False -> illegal cancels are skipped).
    for enr in _governing_enrollments(client):
        if EnrollmentStage(enr.stage) in _TERMINAL_STAGES:
            continue
        try:
            advance_enrollment(
                enr,
                EnrollmentStage.CANCELLED,
                actor=actor,
                actor_label=actor_label,
                note=_CLOSURE_CANCEL_NOTE,
            )
            result["cancelled"] = True
        except InvalidTransition:
            pass

    if result["cancelled"]:
        _write_primary_system_note(
            client,
            (
                f"Member cancelled on {today}: no open internal-service case "
                f"remains after case {governing.case_id} closed."
            ),
            author_name=author,
        )
    return result


def _internal_service_cases(client):
    return [c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE]


def open_internal_service_cases(client):
    """Internal-service cases that are still OPEN (not closed/cancelled)."""
    if client is None:
        return []
    return [
        c for c in _internal_service_cases(client)
        if c.case_status not in _CLOSED_CASE_STATUSES
    ]


def pending_switch_case(enrollment, governing_kind=None):
    """An OPEN internal-service case with a PENDING authorization -- i.e. an
    in-flight authorization the agent is waiting on (a product switch being
    approved, or a renewal). Returns the case, or None.

    When ``governing_kind`` is given, only a DIFFERENT-kind pending case counts
    (a genuine meals<->boxes switch); otherwise any pending open case counts
    (also covers a same-kind renewal). Used to (a) keep serving the current kind
    during the gap rather than truncating, and (b) surface ``program_switch_pending``
    in PO Blockers.
    """
    if enrollment is None:
        return None
    from api.services.catalog import product_type_kind_for_name

    for c in open_internal_service_cases(enrollment.client):
        if c.service_authorization_status != ServiceAuthorizationStatus.PENDING:
            continue
        if governing_kind is None:
            return c
        k = product_type_kind_for_name(c.program_name)
        if k is not None and k != governing_kind:
            return c
    return None


def reconcile_delivery_state(enrollment, *, actor=None):
    """Keep an active enrollment's delivery calendar consistent after a case /
    authorization change -- WITHOUT ever auto-switching product kind.

    * Governing case is approved + open with a future window AND the product kind
      is unchanged -> auto-heal a window drift (extend/adjust dates) via
      :func:`orders.heal_delivery_window`. A meals<->boxes switch is left for the
      human-confirmed ``program_switched`` fix in PO Blockers.
    * No favorable open authorization covers the future:
        - an in-flight switch/renewal is pending (open PENDING case) -> keep
          serving the current kind (no change), surfaced as ``program_switch_pending``;
        - otherwise -> truncate future non-batched occurrences to today via
          :func:`orders.truncate_future_deliveries` so a closed/expired case stops
          over-delivering.

    Best-effort and idempotent; heavy work only runs when something actually
    drifted. Terminal / on-hold enrollments are skipped.
    """
    from api.models import ScheduleStatus
    from api.services.orders import heal_delivery_window, truncate_future_deliveries

    if EnrollmentStage(enrollment.stage) in _TERMINAL_STAGES:
        return
    if EnrollmentStage(enrollment.stage) == EnrollmentStage.ON_HOLD:
        return
    if not enrollment.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED).exists():
        return

    gov = governing_internal_case(enrollment)
    end = getattr(gov, "service_authorization_approval_ends_at", None) if gov else None
    gov_end = end.date() if end else None
    favorable = gov is not None and gov.service_authorization_status in (
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    )
    is_open = gov is not None and gov.case_status not in _CLOSED_CASE_STATUSES
    today = timezone.localdate()
    authorizes_future = favorable and is_open and gov_end is not None and gov_end >= today

    if authorizes_future:
        # Same-kind window drift heals automatically; a kind switch is a no-op
        # here (heal_delivery_window refuses to flip kind) and waits for the
        # human-confirmed PO Blockers fix.
        heal_delivery_window(enrollment)
        return

    # No open+approved authorization covering the future. Keep serving through an
    # in-flight switch/renewal ONLY when the household is currently authorized by
    # an OPEN approved case (its window merely drifted/expired) -- that is the
    # legitimate gap-serving case. When NO open approved authorization exists
    # anywhere (the household's only open case is pending -- an initial request
    # not yet granted -- or its sole approval sits on a CLOSED case), a pending
    # case is NOT a renewal of live service: service must not run, so truncate.
    # This aligns delivery with the PO guardrail (authorized == OPEN + APPROVED).
    has_open_approved = any(
        c.service_authorization_status in (
            ServiceAuthorizationStatus.APPROVED,
            ServiceAuthorizationStatus.NOT_REQUIRED,
        )
        and c.case_status not in _CLOSED_CASE_STATUSES
        for c in _internal_service_cases(enrollment.client)
    )
    if has_open_approved and pending_switch_case(enrollment) is not None:
        return
    truncate_future_deliveries(enrollment)


def _resume_auto_paused_enrollment(enrollment, *, actor=None):
    """Resume an enrollment that THIS rule auto-paused (ON_HOLD) back to the
    stage it was held from. No-op when the most recent hold was NOT an auto-pause
    (so a manual Place-on-Hold is never silently overridden)."""
    last_hold = (
        StageEvent.objects.filter(
            enrollment=enrollment, to_stage=EnrollmentStage.ON_HOLD
        )
        .order_by("-entered_at")
        .first()
    )
    if not last_hold or not (last_hold.note or "").startswith(_DENIAL_HOLD_NOTE):
        return enrollment
    target = EnrollmentStage.KITCHEN_ASSIGNMENT
    if last_hold.from_stage:
        try:
            target = EnrollmentStage(last_hold.from_stage)
        except ValueError:
            target = EnrollmentStage.KITCHEN_ASSIGNMENT
    try:
        return advance_enrollment(
            enrollment, target, actor=actor, force=True,
            note="Auto-resumed: internal-service case re-approved.",
        )
    except InvalidTransition:
        return enrollment


# When set, ``CaseSerializer`` SKIPS its inline per-save call to
# ``reconcile_internal_service_authorization`` (below). Imports process one case
# per row, so a client with several cases would otherwise have the client-wide
# reconcile fire against a partial picture -- e.g. cancelling a household when
# the row for its still-open case hasn't been written yet. Imports set this for
# the duration of the case load, then run the reconcile ONCE per client on the
# complete picture. Single-case writes (extension/portal) leave it False so they
# reconcile immediately.
_DEFER_INTERNAL_SERVICE_RECONCILE = contextvars.ContextVar(
    "defer_internal_service_reconcile", default=False
)


def internal_service_reconcile_deferred():
    """True when a caller (an import) has deferred the per-save reconcile."""
    return _DEFER_INTERNAL_SERVICE_RECONCILE.get()


@contextmanager
def deferred_internal_service_reconcile():
    """Within this context, ``CaseSerializer`` skips its inline reconcile; the
    caller is responsible for running ``reconcile_internal_service_authorization``
    once per touched client afterwards, on the full case picture."""
    token = _DEFER_INTERNAL_SERVICE_RECONCILE.set(True)
    try:
        yield
    finally:
        _DEFER_INTERNAL_SERVICE_RECONCILE.reset(token)


def reconcile_internal_service_authorization(client, *, actor=None, actor_label=""):
    """React to a change in the client's internal-service case authorization.

    Full-stop rule: a client whose GOVERNING internal-service (meal/box)
    authorization is DENIED is paused -- every post-verification enrollment is
    moved to On Hold (mirrors the manual Place-on-Hold), which drops them off the
    kitchen-assignment queue and pauses delivery. Reversible: when the governing
    internal-service authorization later becomes favorable (the case is
    re-approved, or an approved case supersedes it) the enrollment auto-resumes to
    the stage it was held from.

    Because ``governing_case_key`` ranks an approval/pending case ABOVE a denial,
    "the governing case is denied" already means NO approved/pending meal/box
    program exists -- so this fires whether the client has one denied case or
    several (e.g. two parallel cases that were BOTH denied, possibly on different
    days). An approved/pending parallel program keeps the household in service
    (it becomes the governing case, so gov_status is not DENIED).

    Best-effort and idempotent. Returns ``{"sole_denied", "paused",
    "closed_out", "cancelled"}``.
    """
    result = {
        "sole_denied": False, "paused": False,
        "closed_out": False, "cancelled": False, "downgraded": False,
    }
    cases = _internal_service_cases(client)
    if not cases:
        recompute_client_stage(client, actor=actor)
        return result

    open_cases = open_internal_service_cases(client)
    governing = max(cases, key=governing_case_key)
    gov_status = governing.service_authorization_status

    if not open_cases:
        # CLOSURE full stop: the client's LAST open internal-service case has
        # closed. Pause + truncate future deliveries + note the primary, THEN
        # cancel + a second note. Opens NO tickets -- the timeline, StageEvents
        # and primary notes carry the visibility.
        outcome = _full_stop_close_out(
            client, governing, actor=actor, actor_label=actor_label,
        )
        result["closed_out"] = True
        result["paused"] = outcome["paused"]
        result["cancelled"] = outcome["cancelled"]
    elif gov_status == ServiceAuthorizationStatus.DENIED:
        # Governing meal/box authorization is denied (no favorable/pending open
        # case exists, whether one case or several) -> full stop: pause every
        # servable enrollment (incl. Active -- Rule 3).
        result["sole_denied"] = True
        for enr in _governing_enrollments(client):
            if EnrollmentStage(enr.stage) in _DENIAL_PAUSE_STAGES:
                try:
                    advance_enrollment(
                        enr, EnrollmentStage.ON_HOLD, actor=actor,
                        actor_label=actor_label, note=_DENIAL_HOLD_NOTE,
                    )
                    result["paused"] = True
                except InvalidTransition:
                    pass
    elif gov_status in (
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    ):
        # Favorable authorization -> resume anything this rule auto-paused AND
        # advance a verified household to Kitchen Assignment (Rule 2). Routing
        # the advance through here means it fires on EVERY case-save path
        # (extension, manual import, bulk CLI), not just the manual import.
        for enr in _governing_enrollments(client):
            if EnrollmentStage(enr.stage) == EnrollmentStage.ON_HOLD:
                _resume_auto_paused_enrollment(enr, actor=actor)
            else:
                try:
                    reconcile_enrollment_authorization(
                        enr, actor=actor, actor_label=actor_label,
                    )
                except Exception:  # pragma: no cover - defensive
                    pass
    elif gov_status in _WAITING_AUTH_STATUSES:
        # Not approved yet (pending / never-requested / blank) but an open case
        # exists: only an approval authorizes service, so pull any enrollment
        # that was advanced past Verified BACK to Verified ("Waiting
        # Authorization") and stop its future deliveries. Fixes households that
        # were activated before their authorization was approved (the CSV-import
        # gap). Fires on every case-save path via this single chokepoint, and
        # auto-resumes when the case is later approved.
        for enr in _governing_enrollments(client):
            if EnrollmentStage(enr.stage) in _UNAUTH_PULLBACK_STAGES:
                _downgrade_unauthorized_enrollment(
                    enr, actor=actor, actor_label=actor_label,
                )
                result["downgraded"] = True

    # Keep the delivery calendar in step with the (possibly changed) governing
    # authorization: auto-heal a same-kind window extension, or truncate future
    # deliveries when a case closed with no authorization / pending successor.
    # Never auto-flips product kind -- a meals<->boxes switch stays a
    # human-confirmed PO Blockers fix. Skipped on a close-out (deliveries were
    # already truncated and the enrollments cancelled). Best-effort.
    if not result["closed_out"]:
        for enr in _governing_enrollments(client):
            try:
                reconcile_delivery_state(enr, actor=actor)
            except Exception:  # pragma: no cover - defensive
                pass

    recompute_client_stage(client, actor=actor)

    # Refresh the member/household warning snapshot after a case-driven change
    # (fires on both extension case saves and CSV imports, which route through
    # CaseSerializer -> here). Best-effort; lazy import avoids a circular dep.
    try:
        from api.services.warnings import sync_client_warnings

        sync_client_warnings(client)
    except Exception:  # pragma: no cover - defensive
        pass

    return result

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
    # A DISREGARDED enrollment is a dismissed verification request, and a
    # SCHEDULED_EXTENSION enrollment is a parked reauthorization not yet serving:
    # neither governs the client's stage (the member reflects their live
    # enrollment). The scheduled extension is activated by its own explicit path
    # (see the reauthorization activation task), not the generic governance here.
    _inert = {EnrollmentStage.DISREGARDED, EnrollmentStage.SCHEDULED_EXTENSION}
    return [e for e in seen.values() if EnrollmentStage(e.stage) not in _inert]


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

    Split-household guard: once a member holds their OWN live enrollment (a
    dependent split into their own case), a HOUSEHOLD-MATE's individual pending
    enrollment must NOT mark them pending -- their own (verified) enrollment
    governs them. Household inheritance still applies to a TRUE dependent (no own
    live enrollment) so a shared verification keeps moving everyone together.
    """
    candidates = [
        e
        for e in _governing_enrollments(client)
        if e.verified_at is None
        and EnrollmentStage(e.stage) == EnrollmentStage.PENDING_VERIFICATION
    ]
    if not candidates:
        return None
    # A member who is themselves an enrollment holder (has their OWN live
    # enrollment) is governed by their own enrollment -- ignore a household-mate's
    # separate-case pending row (the split-household leak).
    own_live = any(
        EnrollmentStage(e.stage) not in _TERMINAL_STAGES
        and EnrollmentStage(e.stage) not in (
            EnrollmentStage.DISREGARDED, EnrollmentStage.SCHEDULED_EXTENSION,
        )
        for e in client.enrollments.all()
    )
    if own_live:
        own = [e for e in candidates if str(e.client_id) == str(client.pk)]
        candidates = own or []
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


def refresh_internal_case_sort(client, *, save=True):
    """Refresh the two denormalized Members-list case-date keys:

    * ``internal_case_opened_at``           = MOST RECENT internal-service case
      ``date_opened`` (the "Created" SORT key), and
    * ``governing_internal_case_opened_at`` = the GOVERNING internal-service
      case's ``date_opened`` (the "Created" FILTER key -- favorability/deferral
      aware, matching the Data page's governing-case semantics).

    Denormalized so the list can ORDER BY / range-filter an indexed column
    (index scan + LIMIT) instead of a correlated subquery per client. No-op when
    both values are already current."""
    from django.db.models import Max

    from api.models import Case, CaseType
    from api.portal.serializers import (
        active_enrollment,
        governing_service_case_for_display,
    )

    latest = Case.objects.filter(
        client=client, case_type=CaseType.INTERNAL_SERVICE,
    ).aggregate(m=Max("date_opened"))["m"]
    gov = governing_service_case_for_display(client)
    # Mirror the Data page: when there is NO governing internal-service case the
    # member is "No Case" and all case/enrollment-derived dates are blank (a
    # caseless enrollment must not leak a requested/completed date). Only populate
    # the governing enrollment dates when a governing case exists.
    if gov is None:
        gov_opened = gov_requested = gov_completed = None
    else:
        gov_opened = gov.date_opened
        enr = active_enrollment(client)
        gov_requested = (enr.requested_at or enr.opened_at) if enr is not None else None
        gov_completed = enr.verified_at if enr is not None else None

    updates = {
        "internal_case_opened_at": latest,
        "governing_internal_case_opened_at": gov_opened,
        "governing_verification_requested_at": gov_requested,
        "governing_verification_completed_at": gov_completed,
    }
    fields = [f for f, val in updates.items() if getattr(client, f) != val]
    for f in fields:
        setattr(client, f, updates[f])
    if fields and save:
        client.save(update_fields=fields)
    return latest


def stage_event_actor(actor):
    """``StageEvent.actor`` is a FK to the Django auth User, but portal/reconcile
    callers routinely pass the DRF ``AgentUser`` (a lightweight principal, NOT a
    DB user) or an ``Agent`` model. Assigning any of those to the FK raises
    ``ValueError: ... must be a "User" instance`` and aborts the stage change
    (and, via reconcile, the whole case upsert). Coerce anything that isn't a
    saved auth-User instance to None so the audit row still writes -- the acting
    agent's identity is preserved via the note/label + timeline event."""
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    if isinstance(actor, user_model) and getattr(actor, "pk", None) is not None:
        return actor
    return None


def _set_client_stage(client, target, *, actor=None):
    """Set the client's lifecycle stage + log a StageEvent, unconditionally
    (used for the SERVICE_INACTIVE off-ramp, which ``derive_client_stage`` never
    PRODUCES -- it only keeps it sticky once set). No-op when already on
    ``target``."""
    current = client.lifecycle_stage
    if current == target:
        return
    client.lifecycle_stage = target
    client.lifecycle_stage_at = timezone.now()
    client.save(update_fields=["lifecycle_stage", "lifecycle_stage_at"])
    StageEvent.objects.create(
        entity_type=StageEntityType.CLIENT,
        client=client,
        from_stage=current or "",
        to_stage=target,
        source=StageEventSource.AUTO,
        actor=stage_event_actor(actor),
    )


@transaction.atomic
def recompute_client_stage(client, *, actor=None, save=True, ignore_sticky=False):
    """Derive and apply the client's funnel stage. Logs a StageEvent only when
    the stage changes. Returns the (possibly unchanged) stage.

    ``ignore_sticky=True`` bypasses the INELIGIBLE / SERVICE_INACTIVE off-ramp
    stickiness so a recovered member is re-derived from live data -- used by the
    reactivation path when a new open internal-service case reopens service.
    """
    target = derive_client_stage(client, ignore_sticky=ignore_sticky)
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
        actor=stage_event_actor(actor),
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
        # A reauthorization case's fresh enrollment is PARKED as a scheduled
        # extension (verification carried from the household's live enrollment).
        EnrollmentStage.SCHEDULED_EXTENSION,
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
        # An already-verified household's reauthorization can be parked directly.
        EnrollmentStage.SCHEDULED_EXTENSION,
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
        # Meals<->boxes program switch requeue (task 3.1, _handle_program_switch):
        # when the governing internal-service case switches product KIND, the
        # active household's kitchen + calendar are for the WRONG product, so it
        # is requeued for a NEW kitchen assignment (kitchen + cadence + calendar).
        EnrollmentStage.KITCHEN_ASSIGNMENT,
    },
    EnrollmentStage.SERVICE_COMPLETE: {EnrollmentStage.CLOSED},
    EnrollmentStage.ON_HOLD: {
        EnrollmentStage.PENDING_VALIDATION,
        EnrollmentStage.VALIDATED,
        EnrollmentStage.PENDING_VERIFICATION,
        EnrollmentStage.VERIFIED,
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.CLOSED,
        EnrollmentStage.CANCELLED,
    },
    EnrollmentStage.CLOSED: set(),
    EnrollmentStage.CANCELLED: set(),
    # Disregarded is non-terminal: a re-request moves it back to pending
    # verification (the ext normally creates a fresh enrollment instead).
    EnrollmentStage.DISREGARDED: {
        EnrollmentStage.PENDING_VERIFICATION,
    },
    # A parked reauthorization extension activates to Service Active (via Kitchen
    # Assignment when the kitchen must be (re)confirmed), can be held, or be
    # cancelled/closed if the reauth case never activates.
    EnrollmentStage.SCHEDULED_EXTENSION: {
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.ON_HOLD,
        EnrollmentStage.CLOSED,
        EnrollmentStage.CANCELLED,
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
def advance_enrollment(enrollment, to_stage, *, actor=None, actor_label="", note="",
                       force=False, trigger=""):
    """Move an enrollment to ``to_stage`` with guard checks. Logs a StageEvent.

    Raises :class:`InvalidTransition` for illegal transitions or unmet process
    gates. Pass ``force=True`` to bypass the gate checks (still validates the
    transition map unless the current stage is terminal).

    ``actor`` is the acting ``User`` (recorded on ``StageEvent.actor``).
    ``actor_label`` is a free-form display string for callers whose actor isn't
    a User (e.g. the support portal, where the actor is an ``Agent``): it drives
    the timeline event's ``actor`` and is stored on ``StageEvent.metadata`` for
    audit, so the history shows WHO advanced the enrollment.
    ``trigger`` is a short machine code for WHAT caused the change (e.g.
    ``"import.governing_denied"``, ``"eligibility.coverage_expired"``,
    ``"case_replaced"``, ``"manual.hold"``). Stored on the StageEvent + mirrored
    into the timeline event metadata so the history is traceable to its cause.
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

    # Entering a terminal stage (Closed / Cancelled) STOPS service: shorten the
    # delivery plans and drop future non-batched calendar occurrences so a dead
    # enrollment can never keep a live delivery calendar. Without this, closing
    # the old enrollment when a governing case is replaced left its calendar
    # scheduled -- making the member read as served by a SECOND kitchen (the old
    # enrollment's). Centralized here so EVERY close path is covered, not just
    # the ones that remember to clean up. Only on the ENTRY transition; batched
    # (PO-committed) occurrences are preserved by sync_delivery_calendar.
    # Best-effort: a cleanup hiccup must never roll back the stage change.
    if to_stage in _TERMINAL_STAGES and from_stage not in _TERMINAL_STAGES:
        try:
            from api.services.orders import truncate_future_deliveries

            truncate_future_deliveries(enrollment)
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger(__name__).warning(
                "terminal calendar cleanup failed for enrollment %s",
                enrollment.pk, exc_info=True,
            )

    # Verification complete -> clear the "new client needs attention" flag. The
    # flag was set when the client's first internal-service case was created
    # (see CaseSerializer); reaching VERIFIED (or beyond) means the verification
    # it was tracking is done, so drop them off the "Need Attention" list.
    if to_stage in _VERIFIED_OR_BEYOND:
        client = enrollment.client
        if client is not None and client.is_new:
            client.is_new = False
            client.save(update_fields=["is_new"])

    stage_meta = {}
    if actor_label:
        stage_meta["actor_label"] = actor_label
    if trigger:
        stage_meta["trigger"] = trigger
    # A change with no human actor/label -- or one whose label is a "system:..."
    # marker (import, reconcile, eligibility off-ramp) -- is system-driven;
    # otherwise it was a person via the portal/admin.
    is_system = actor is None and (
        not actor_label or actor_label.strip().lower().startswith("system:")
    )
    stage_event = StageEvent.objects.create(
        entity_type=StageEntityType.ENROLLMENT,
        enrollment=enrollment,
        client=enrollment.client,
        from_stage=from_stage,
        to_stage=to_stage,
        source=StageEventSource.AUTO if is_system else StageEventSource.MANUAL,
        actor=stage_event_actor(actor),
        note=note,
        metadata=stage_meta,
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
            enrollment, stage_event=stage_event, actor=actor_name or "",
            trigger=trigger,
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
# NEVER_REQUESTED is treated exactly like a DENIAL (same rank): an OPEN case that
# never had an authorization requested confers no service, so it must never be
# chosen as governing over a real approved/pending case, and when it IS the top
# case it drives the same full-stop as a denial (see _DENIED_EQUIVALENT_STATUSES).
_AUTH_FAVOR_RANK = {
    ServiceAuthorizationStatus.APPROVED: 4,
    ServiceAuthorizationStatus.NOT_REQUIRED: 4,
    ServiceAuthorizationStatus.PENDING: 3,
    ServiceAuthorizationStatus.DENIED: 2,
    ServiceAuthorizationStatus.NEVER_REQUESTED: 2,
    ServiceAuthorizationStatus.EXPIRED: 1,
}

# Authorization statuses that drive the DENIAL full-stop. A NEVER_REQUESTED
# authorization on an OPEN case is treated identically to an explicit DENIAL:
# the case stays open but confers no service, so the household is paused / the
# verification request disregarded / a Kitchen-Assignment household off-ramped,
# exactly as a denial would. (Only the DISPLAY label stays "Never Requested".)
_DENIED_EQUIVALENT_STATUSES = {
    ServiceAuthorizationStatus.DENIED,
    ServiceAuthorizationStatus.NEVER_REQUESTED,
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
    matter the dates -- "Approved is a must over any other rule"), then OPEN over
    closed/cancelled (so a lingering superseded case can't keep governing a
    switch), then **most recently created** (``case_created_at`` -- the
    authoritative source created timestamp WITH TIME, since most cases are
    created the same day; falls back to ``date_opened`` only for rows not yet
    refreshed by an import), then most recently updated, then case_id -- a
    stable, environment-independent final tiebreak so the same case is chosen
    everywhere. ``date_opened`` is NOT used as the primary key: it can be
    date-only and agent-edited, which left exact ties to arbitrary DB row order.

    The open-ness rank sits BELOW authorization favor, so an approved case
    (open or closed) still beats a pending one -- during a meals->boxes switch
    the still-approved meals case keeps governing until the boxes case is
    approved, and among two approved cases the open (newer) one wins.
    """
    return (
        _AUTH_FAVOR_RANK.get(case.service_authorization_status, 0),
        _case_open_rank(case),
        case.case_created_at or case.date_opened or _DT_FLOOR,
        case.updated_at or _DT_FLOOR,
        str(case.case_id),
    )


def _case_product_kind(case):
    """Product kind (Meals / Boxes) derived from a case's program name /
    service_type, or None when it can't be resolved."""
    from api.services.catalog import product_type_kind_for_name
    return product_type_kind_for_name(
        (case.program_name or "").strip() or (case.service_type or "").strip()
    )


def deferred_extension_case_ids(cases, *, today=None):
    """Case ids of REAUTHORIZATION extensions whose activation is still in the
    FUTURE and must NOT yet govern -- so the currently-serving case keeps
    governing until the extension's window begins (see
    docs/reauthorization_extension_plan.md).

    A case ``c`` is deferred while ``today < max(E1, S2)`` and ALL hold:
      * ``c.is_extension`` and its authorization is APPROVED / NOT_REQUIRED,
      * its effective window START ``S2`` is in the FUTURE, and
      * there is ANOTHER approved internal-service case of the SAME product kind
        AND SAME scope (household/individual) -- the service being extended.

    The switch point is ``max(E1, S2)`` (``E1`` = the current same-kind/scope
    case's window END), so on an OVERLAP (``S2 < E1``) the extension keeps
    deferring until the current window actually ends -- matching the daily
    activation task (``process_scheduled_extensions``). A different-kind /
    different-scope reauth, or one with no window / a past start, is NOT deferred.
    """
    today = today or timezone.localdate()
    favorable = {
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    }
    deferred = set()
    for c in cases:
        if not getattr(c, "is_extension", False):
            continue
        if c.service_authorization_status not in favorable:
            continue
        # A closed/cancelled reauth case never activates -> never defer/park it.
        if c.case_status in _CLOSED_CASE_STATUSES:
            continue
        start, _end = c.effective_authorization_window()
        if start is None:
            continue  # no window -> can't defer, switch per the normal rule
        kind_c = _case_product_kind(c)
        scope_c = c.household_type
        # Same-scope, OPEN, approved case(s) currently being served -- the
        # program(s) this reauth could extend/replace. Must be OPEN: you can only
        # defer a reauth that extends a CURRENTLY-SERVING program.
        currents = [
            other for other in cases
            if other.case_id != c.case_id
            and other.service_authorization_status in favorable
            and other.case_status not in _CLOSED_CASE_STATUSES
            and other.household_type == scope_c
        ]
        if not currents:
            continue
        start_date = start.date()
        same_kind = [o for o in currents if _case_product_kind(o) == kind_c]
        if same_kind:
            # SAME-kind reauth (e.g. Meals reauth extending the served Meals):
            # switch point = max(E1, S2). Defer while today hasn't reached it; on
            # an overlap this keeps deferring past S2 until the current window
            # ends (E1).
            boundaries = [start_date]
            for other in same_kind:
                _os, oe = other.effective_authorization_window()
                if oe is not None:
                    boundaries.append(oe.date())
            if today < max(boundaries):
                deferred.add(c.case_id)
        elif today < start_date:
            # DIFFERENT-kind but FUTURE-dated switch (e.g. Meals -> Boxes starting
            # next month) behind a currently-serving case: defer so the future
            # case does NOT supplant the serving case until its window actually
            # opens. An immediate/past-start different-kind switch is NOT deferred
            # (start_date <= today) -- it governs now, per the normal rule.
            deferred.add(c.case_id)
    return deferred


def pick_governing_case(cases, *, today=None):
    """The governing internal-service case, EXCLUDING deferred future
    reauthorization extensions (which must not supplant the serving case until
    their window begins). Falls back to the full set if every candidate is a
    deferred extension. Returns None for an empty input."""
    cases = list(cases)
    if not cases:
        return None
    deferred = deferred_extension_case_ids(cases, today=today)
    pool = [c for c in cases if c.case_id not in deferred] or cases
    return max(pool, key=governing_case_key)


def governing_internal_case(enrollment):
    """The internal-service case whose authorization governs this enrollment.

    A household can accumulate several internal-service cases (parallel meal/box
    programs, or a denial later followed by a re-approval). The governing one is
    chosen by :func:`governing_case_key` -- an approved authorization wins over a
    denied one regardless of dates -- not whichever case happens to sit on
    ``enrollment.case`` (which may be stale/superseded). A future-dated
    reauthorization extension is deferred (see :func:`pick_governing_case`) so it
    doesn't prematurely supplant the serving case. Falls back to
    ``enrollment.case`` when the client has no internal-service case.
    """
    client = enrollment.client
    if client is not None:
        cases = [
            c for c in client.cases.all()
            if c.case_type == CaseType.INTERNAL_SERVICE
        ]
        if cases:
            return pick_governing_case(cases)
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

    # A parked reauthorization extension (verified, waiting for its window) reads
    # as "Reauthorization" -- shown read-only on the Programs tab.
    if stage == EnrollmentStage.SCHEDULED_EXTENSION:
        return ProgramStatus.REAUTHORIZATION

    # A paused program shows On Hold above everything else -- UNLESS the hold is
    # a delivery-coverage (Out of Range) hold, which surfaces as Out of Range so
    # the program stage matches the members' Out of Range labels. (The main
    # lifecycle stage separately carries the Ineligible / Does Not Qualify
    # off-ramp; this per-program status reflects the coverage block distinctly.)
    if stage == EnrollmentStage.ON_HOLD:
        if _enrollment_has_out_of_range_member(enrollment):
            return ProgramStatus.OUT_OF_RANGE
        # A hold whose governing case is CLOSED/CANCELLED is the closure
        # full-stop (the last open case closed -> the enrollment is parked On
        # Hold and the client at Service Inactive), NOT a manual/temporary hold.
        # Surface the terminal Closed status so the member list + Program tab
        # don't read a confusing "On Hold" over a closed program. Display-only:
        # if a case reopens, this re-derives back to the live status.
        gov_hold = enrollment.case or governing_internal_case(enrollment)
        if gov_hold and gov_hold.case_status in _CLOSED_CASE_STATUSES:
            return ProgramStatus.CLOSED
        return ProgramStatus.ON_HOLD

    # Each enrollment is bound to one case; terminal enrollments in particular
    # must report their OWN case's status, not the household's current governing.
    gov = enrollment.case or governing_internal_case(enrollment)
    auth = getattr(gov, "service_authorization_status", "") if gov else ""
    case_closed = bool(gov and gov.case_status in _CLOSED_CASE_STATUSES)
    end = getattr(gov, "service_authorization_approval_ends_at", None) if gov else None
    window_expired = bool(end and end.date() < timezone.localdate())
    auth_expired = auth == ServiceAuthorizationStatus.EXPIRED or window_expired

    # A reauthorization GAP pause: the current window ended and the household is
    # completed + paused awaiting the reauth window. Surface "Reauthorization"
    # (not a generic Closed) so it's clear service will resume. Display-only.
    if (
        stage == EnrollmentStage.SERVICE_COMPLETE
        and enrollment.close_reason == "reauth_gap"
        and not case_closed
    ):
        return ProgramStatus.REAUTHORIZATION

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
        # Nutritionist gate sits between Verified and Kitchen Assignment. Until a
        # Nutritionist signs off, the household is Pending Nutritionist regardless
        # of the authorization outcome.
        if not enrollment.nutritionist_approved_at:
            return ProgramStatus.PENDING_NUTRITIONIST
        # Nutritionist-approved. An approved authorization advances the stage to
        # Kitchen Assignment (reconcile), so at VERIFIED it only rests here while
        # the authorization is still pending/denied/blank.
        if auth == ServiceAuthorizationStatus.DENIED:
            return ProgramStatus.DENIED
        if auth in (
            ServiceAuthorizationStatus.APPROVED,
            ServiceAuthorizationStatus.NOT_REQUIRED,
        ):
            return ProgramStatus.AUTHORIZED
        # Pending / blank authorization: nutritionist done, waiting on auth.
        return ProgramStatus.NUTRITIONIST_APPROVED

    return ProgramStatus.PENDING_VERIFICATION


# ---------------------------------------------------------------------------
# Phase 7: per-program display tracks for the new member stage bar
# ---------------------------------------------------------------------------
# The redesigned stage bar decomposes each PROGRAM into three display phases --
# Authorization -> Verification -> Service -- rather than the single linear
# ``program_status`` timeline. ``program_tracks`` returns one entry per distinct
# program the client qualifies for. It is generic across categories (today only
# Food -> Meals/Boxes exists, but additional categories slot in as more entries)
# and is derived on read, never stored.


def _authorization_phase(auth):
    """Fold a Case ``service_authorization_status`` into the display states of
    the Authorization phase.

    The bar shows the REAL authorization state rather than collapsing every
    not-yet-decided value into a generic "Waiting Authorization" (which was
    confusing -- a case where an authorization was never even requested read the
    same as one with a live pending request). States:

        Approved / Denied / Never Requested / Requested
    """
    from api.models import ServiceAuthorizationStatus as A

    # EXPIRED was authorized once; the expiry surfaces in the Service phase, so
    # the Authorization phase still reads Approved.
    if auth in (A.APPROVED, A.NOT_REQUIRED, A.EXPIRED):
        return ("approved", "Approved")
    if auth == A.DENIED:
        return ("denied", "Denied")
    # No authorization was ever requested on the case (an OPEN case with a blank
    # authorization -- see csv_import). Distinct from a live pending request.
    if auth == A.NEVER_REQUESTED:
        return ("never_requested", "Never Requested")
    # PENDING (Unite Us requested / open / deferred) -- a request exists and is
    # awaiting a decision. A truly blank status also lands here.
    return ("requested", "Requested")


def _verification_phase(enrollment):
    """Verification phase for the program bar:

        Not Requested -> no verification request has been raised yet
        Pending Verification -> a request exists but isn't Verified yet
        Verified -> the enrollment has been verified

    "Pending Verification" is reserved for a LIVE request (an enrollment exists
    and sits in a pre-verification stage). A food case with NO enrollment has no
    request yet, so it reads "Not Requested" -- mirroring the Authorization
    phase's "Never Requested" -- instead of implying a pending request that the
    verification button (which needs a real request) won't offer.
    """
    if enrollment is None:
        return ("not_requested", "Not Requested")
    if EnrollmentStage(enrollment.stage) in _PRE_VERIFICATION_STAGES:
        return ("pending", "Pending Verification")
    return ("verified", "Verified")


def _nutritionist_phase(enrollment):
    """Nutritionist sign-off phase for the program bar -- sits between
    Verification and Authorization. Blank until the household is verified.

        Pending Nutritionist -> verified, awaiting the Nutritionist's sign-off
        Nutritionist Approved -> a Nutritionist has signed off
    """
    if enrollment is None:
        return ("", "")
    if EnrollmentStage(enrollment.stage) in _PRE_VERIFICATION_STAGES:
        return ("", "")  # not verified yet -> the nutritionist step isn't reached
    if enrollment.nutritionist_approved_at:
        # Only a REAL sign-off (nutritionist_approved_by set) reads "Nutritionist
        # Approved". Grandfathered households (back-stamped approved_at with no
        # approved_by, verified before the gate launched) never went through the
        # step, so the node stays blank.
        if enrollment.nutritionist_approved_by_id:
            return ("approved", "Nutritionist Approved")
        return ("", "")
    return ("pending", "Pending Nutritionist")


def _member_status_on(client, enrollment):
    """The client's own per-member status on the given enrollment (or None)."""
    if enrollment is None or client is None:
        return None
    for mv in enrollment.member_profiles.all():
        if mv.client_id and str(mv.client_id) == str(client.client_id):
            return mv.status
    return None


def _enrollment_has_out_of_range_member(enrollment):
    """True when any member of ``enrollment`` is Out of Range -- i.e. the
    household is held for a delivery-coverage (ZIP) block, which the program
    stage should surface as Out of Range rather than a generic On Hold."""
    from api.models import MemberStatus

    if enrollment is None:
        return False
    return any(
        mv.status == MemberStatus.OUT_OF_RANGE
        for mv in enrollment.member_profiles.all()
    )


def _service_phase(client, enrollment, gov_case):
    """Service phase display status. Empty until the program reaches a servicing
    stage. Mirrors the plan's Service statuses (Waiting for Kitchen Assignment /
    Active / On Hold / Out of Range / Out of Orbit / Service Expired / Canceled /
    Does Not Qualify / Closed)."""
    from api.models import CaseStatus, ClientStage, MemberStatus
    from api.models import ServiceAuthorizationStatus as A

    # A terminal GOVERNING case (now surfaced on the bar even when closed) ends
    # the program, so the Service phase reflects the CASE outcome directly --
    # Closed vs Canceled -- taking precedence over the enrollment stage (a
    # closed case can sit atop a cancelled or completed enrollment).
    case_status = getattr(gov_case, "case_status", None) if gov_case else None
    if case_status == CaseStatus.CANCELLED:
        return ("canceled", "Canceled")
    if case_status == CaseStatus.CLOSED:
        return ("closed", "Closed")

    if enrollment is not None:
        stage = EnrollmentStage(enrollment.stage)
        if stage == EnrollmentStage.CANCELLED:
            return ("canceled", "Canceled")
        if stage in (EnrollmentStage.CLOSED, EnrollmentStage.SERVICE_COMPLETE):
            return ("closed", "Closed")
        # An Out-of-Range member surfaces on the SERVICE phase as Out of Range on
        # every non-terminal stage -- including an Out-of-Range HOLD (ON_HOLD) --
        # and takes precedence over both On Hold and the Does Not Qualify
        # eligibility off-ramp below: the per-program Service stage reflects the
        # coverage block, while the MAIN lifecycle stage separately carries the
        # Ineligible / Does Not Qualify off-ramp.
        if _member_status_on(client, enrollment) == MemberStatus.OUT_OF_RANGE:
            return ("out_of_range", "Out of Range")
    # Coverage / eligibility off-ramp (Decision 2): unsupported insurance or
    # social-care coverage -> Does Not Qualify. Distinct from the main-bar Not
    # Eligible off-ramp; only applies to a still-live (non-terminal) program.
    if client is not None and client.lifecycle_stage == ClientStage.INELIGIBLE:
        return ("does_not_qualify", "Does Not Qualify")
    if enrollment is None:
        return ("", "")
    stage = EnrollmentStage(enrollment.stage)
    if stage == EnrollmentStage.ON_HOLD:
        return ("on_hold", "On Hold")
    end = getattr(gov_case, "service_authorization_approval_ends_at", None) if gov_case else None
    auth = getattr(gov_case, "service_authorization_status", "") if gov_case else ""
    window_expired = bool(end and end.date() < timezone.localdate())
    if (auth == A.EXPIRED or window_expired) and stage in _AUTH_WINDOW_STAGES:
        return ("service_expired", "Service Expired")
    # A VERIFIED household whose authorization is APPROVED is cleared for service
    # but not yet assigned to a kitchen -> "Waiting for Kitchen Assignment" (the
    # KITCHEN_ASSIGNMENT stage is the explicit next step, but the moment
    # verification completes on an approved case the program is already waiting on
    # a kitchen). If the authorization hasn't been approved yet (still
    # pending/requested) the Authorization phase carries that state and Service
    # stays blank until approval lands.
    # The Nutritionist gate sits before kitchen assignment: a verified household
    # that hasn't been Nutritionist-approved is NOT yet waiting on a kitchen (it's
    # Pending Nutritionist), so the Service phase stays blank until sign-off.
    verified_awaiting_kitchen = (
        stage == EnrollmentStage.VERIFIED
        and auth in (A.APPROVED, A.NOT_REQUIRED)
        and enrollment.nutritionist_approved_at is not None
    )
    if stage in (EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.KITCHEN_ASSIGNMENT) or verified_awaiting_kitchen:
        mv = _member_status_on(client, enrollment)
        if mv == MemberStatus.OUT_OF_RANGE:
            return ("out_of_range", "Out of Range")
        if mv == MemberStatus.OUT_OF_ORBIT:
            return ("out_of_orbit", "Out of Orbit")
        if stage == EnrollmentStage.SERVICE_ACTIVE:
            return ("active", "Active")
        return ("waiting_kitchen", "Kitchen Assignment")
    # Verified but authorization not yet approved -> Service stays blank.
    return ("", "")


def program_tracks(client):
    """Per-program display tracks for the redesigned member stage bar (Phase 7).

    One entry per distinct PROGRAM the client qualifies for -- grouped by product
    kind (Meals/Boxes) today, generic for future categories. Each program's
    governing internal-service case drives the Authorization phase; the client's
    governing (household) enrollment drives the Verification + Service phases. The
    governing program is returned first (``governing: true``), additional programs
    after. Derived on read; never stored. Empty list when the client holds no
    internal-service case.
    """
    from api.models import CaseHouseholdType, ProductTypeKind
    from api.models import ServiceAuthorizationStatus as A
    from api.services.catalog import product_type_kind_for_name

    if client is None:
        return []
    # The GOVERNING internal-service case is ALWAYS surfaced regardless of its
    # case STATUS -- even when closed / cancelled -- so the bar always reflects
    # the case that governs the member (its live Service phase carries Closed /
    # Canceled). Non-governing cases render only while OPEN; a closed/cancelled
    # non-governing case drops off (its history lives on the Programs tab). A
    # NEVER_REQUESTED authorization is treated like a denial and stays hidden for
    # every case (governing included) -- it is not a real program to surface.
    all_cases = _internal_service_cases(client)
    if not all_cases:
        return []
    governing = pick_governing_case(all_cases)
    cases = [
        c for c in all_cases
        if c.service_authorization_status != A.NEVER_REQUESTED
        and (
            c.case_status not in _CLOSED_CASE_STATUSES
            or c.case_id == governing.case_id
        )
    ]

    # Representative enrollment (a verification is household-wide): the most
    # recent non-terminal governing enrollment, else the most recent overall.
    enr = None
    enrollments = _governing_enrollments(client)
    if enrollments:
        live = [
            e for e in enrollments
            if EnrollmentStage(e.stage) not in _TERMINAL_STAGES
        ]
        enr = sorted(
            live or enrollments,
            key=lambda e: e.opened_at or _DT_FLOOR,
            reverse=True,
        )[0]

    # One track PER internal-service case -- cases are NOT grouped by program, so
    # every case shows on the bar (even two of the same Meals/Boxes kind), and
    # the single governing case is flagged (``governing``) so the UI can
    # distinguish it. Authorization is read per-case. Verification + Service
    # attach to the household EnrollmentVerification, which reconciles onto the
    # governing case -- so only the governing FOOD case shows those phases; every
    # other case shows Authorization only, with Verification/Service blank ("--").
    gov_kind = product_type_kind_for_name(
        governing.service_type or governing.program_name
    ) if governing else None
    gov_label = (
        (governing.service_type or governing.service_category
         or governing.program_name or "").strip().casefold()
        if governing else ""
    )
    # Reauth cases parked as a scheduled extension surface as
    # "Reauthorization - Waiting" (not the generic "Duplicated" same-kind label).
    from api.models import EnrollmentVerification as _EV, EnrollmentStage as _ES
    scheduled_ext_case_ids = set(
        _EV.objects
        .filter(client=client, stage=_ES.SCHEDULED_EXTENSION)
        .values_list("case_id", flat=True)
    )
    tracks = []
    for c in cases:
        kind = product_type_kind_for_name(c.service_type or c.program_name)
        is_food = kind is not None
        is_governing = governing is not None and c.case_id == governing.case_id
        if is_food:
            # A "duplicate" is a non-governing case for the SAME food kind; a
            # DIFFERENT-kind non-governing food case CONFLICTS (a household runs
            # one food program, Meals/Boxes being subtypes of it).
            is_duplicate = (not is_governing) and kind == gov_kind
            is_conflicting = (
                (not is_governing) and gov_kind is not None and kind != gov_kind
            )
        else:
            label = (
                c.service_type or c.service_category or c.program_name or ""
            ).strip().casefold()
            is_duplicate = (
                (not is_governing) and gov_kind is None and label == gov_label
            )
            is_conflicting = False
        auth = getattr(c, "service_authorization_status", "") or ""
        a_val, a_lbl = _authorization_phase(auth)
        # Verification is a HOUSEHOLD-WIDE fact (one EnrollmentVerification), so
        # every FOOD case reflects it. Non-food programs model Authorization
        # only, so verification stays blank there.
        v_val, v_lbl = _verification_phase(enr) if is_food else ("", "")
        n_val, n_lbl = _nutritionist_phase(enr) if is_food else ("", "")
        if is_governing and is_food:
            s_val, s_lbl = _service_phase(client, enr, c)
        elif c.case_id in scheduled_ext_case_ids:
            s_val, s_lbl = ("scheduled_extension", "Reauthorization")
        elif is_duplicate:
            s_val, s_lbl = ("duplicated", "Duplicated")
        elif is_conflicting:
            s_val, s_lbl = ("conflicting", "Conflicting")
        else:
            s_val, s_lbl = ("", "")
        if is_food:
            service_type = ProductTypeKind(kind).label
            service_type_value = kind.value
            category = getattr(c, "service_category", "") or "Food"
        else:
            service_type = (
                c.service_type or c.service_category or c.program_name or ""
            ).strip()
            service_type_value = service_type.casefold().replace(" ", "_")
            category = getattr(c, "service_category", "") or service_type
        # Service scope (Household vs Individual) is DRIVEN BY THE CASE -- derived
        # LIVE from the program name (the source of truth), never the stored
        # household_type cache. A manual per-household scope correction lives on
        # the enrollment (household_type_override) and must NEVER change what the
        # governing case reports here.
        from api.serializers import derive_household_type

        ht = derive_household_type(None, getattr(c, "program_name", "")) or CaseHouseholdType.INDIVIDUAL
        tracks.append({
            "category": category,
            "service_type": service_type,
            "service_type_value": service_type_value,
            "case_id": str(c.case_id),
            # Raw case status so the UI can hide close/actions on a case that is
            # already closed/cancelled (a closed governing case still renders).
            "case_status": getattr(c, "case_status", "") or "",
            "governing": is_governing,
            "scope": {"value": ht, "label": CaseHouseholdType(ht).label},
            "authorization": {"value": a_val, "label": a_lbl},
            "verification": {"value": v_val, "label": v_lbl},
            "nutritionist": {"value": n_val, "label": n_lbl},
            "service": {"value": s_val, "label": s_lbl},
        })
    # Governing first, then by service-type label + case id (a stable,
    # environment-independent order).
    tracks.sort(key=lambda t: (
        not t["governing"], t["service_type"], t["case_id"]
    ))
    return tracks


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
    # A hard eligibility off-ramp is the headline outcome: an INELIGIBLE member
    # reads Ineligible on the stage bar's Eligibility node even when a (cancelled
    # or still-live) enrollment exists -- the enrollment roll-up below would
    # otherwise hide it behind Enrolled/Cancelled. INELIGIBLE is sticky (set only
    # by reconcile_client_eligibility), so this reflects the current gate verdict;
    # the per-program Service stage separately carries the service state (Out of
    # Range / Canceled / On Hold).
    if client.lifecycle_stage == ClientStage.INELIGIBLE:
        return ClientStage.INELIGIBLE

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

    if EnrollmentStage(enrollment.stage) not in _AUTH_ELIGIBLE_STAGES:
        return enrollment

    target = _AUTH_STATUS_TO_STAGE.get(case.service_authorization_status)
    if target is None:
        # Not approved (pending / denied / expired / blank): no stage change.
        # The household stays Verified; the status is surfaced from the Case.
        return enrollment

    # Nutritionist sign-off gate: a verified household may NOT advance to kitchen
    # assignment (and thus into service / POs) until a Nutritionist has approved
    # it, even when the case authorization is approved. It waits at Verified
    # (Pending Nutritionist) until then.
    if not enrollment.nutritionist_approved_at:
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


def nutritionist_approve(enrollment, *, agent, signature, signature_image="", pdf_key=""):
    """Record a Nutritionist's legal sign-off on a VERIFIED enrollment (the
    Pending Nutritionist gate), then let an approved authorization advance it to
    Kitchen Assignment.

    Captures the audit trail -- who / when / typed signature / drawn signature /
    generated PDF key -- and emits a timeline event. Idempotent: re-approving an
    already-approved enrollment is a no-op. Returns the (possibly advanced)
    enrollment.
    """
    from api.models import TimelineEventType
    from api.services.timeline import emit_timeline_event

    if enrollment.nutritionist_approved_at:
        return enrollment

    now = timezone.now()
    enrollment.nutritionist_approved_at = now
    enrollment.nutritionist_approved_by = agent
    enrollment.nutritionist_signature = (signature or "").strip()
    enrollment.nutritionist_signature_image = signature_image or ""
    enrollment.nutritionist_approval_pdf_key = pdf_key or ""
    enrollment.save(update_fields=[
        "nutritionist_approved_at", "nutritionist_approved_by",
        "nutritionist_signature", "nutritionist_signature_image",
        "nutritionist_approval_pdf_key",
    ])

    client = getattr(enrollment, "client", None)
    if client is not None:
        # Capture WHAT was reviewed/approved so the event is self-explanatory:
        # the acting Nutritionist + signature/PDF, plus the per-member nutrition
        # data that was signed off (meal plan, meal type, conditions, meds,
        # allergies, weight/height, assessment notes). Best-effort -- never let
        # the summary break the approval.
        review_members = []
        try:
            from api.services.nutrition_pdf import nutrition_review_context

            ctx = nutrition_review_context(enrollment)
            for m in ctx.get("members", []):
                review_members.append({
                    "client_id": m.get("client_id", ""),
                    "name": m.get("name", ""),
                    "status": m.get("status", ""),
                    "meal_plan": m.get("meal_plan", ""),
                    "meal_plan_other": m.get("meal_plan_other", ""),
                    "meal_type": m.get("meal_type", ""),
                    "food_allergies": m.get("food_allergies", []),
                    "conditions": m.get("conditions", []),
                    "medications": m.get("medications", []),
                    "weight": m.get("weight", ""),
                    "height": m.get("height", ""),
                    "on_medical_diet": m.get("on_medical_diet"),
                    "medical_diet_details": m.get("medical_diet_details", ""),
                    "nutrition_concern": m.get("nutrition_concern", ""),  # Primary Nutrition Concern
                    "assessment_notes": m.get("assessment_notes", ""),
                })
        except Exception:  # pragma: no cover - defensive
            review_members = []
        emit_timeline_event(
            client=client,
            event_type=TimelineEventType.NUTRITIONIST_APPROVED,
            occurred_at=now,
            title="Nutritionist Approved",
            subtitle=(
                f"Signed by {enrollment.nutritionist_signature}"
                if enrollment.nutritionist_signature else ""
            ),
            actor=getattr(agent, "name", "") or "",
            enrollment=enrollment,
            case=enrollment.case,
            metadata={
                "signature": enrollment.nutritionist_signature,
                "approved_by": getattr(agent, "name", "") or "",
                "approved_at": now.isoformat(),
                "pdf_key": enrollment.nutritionist_approval_pdf_key or "",
                "members_reviewed": len(review_members),
                "members": review_members,
            },
        )

    # An approved authorization can now advance the enrollment to kitchen
    # assignment (the gate in reconcile_enrollment_authorization is satisfied).
    # actor is a StageEvent User FK (agents aren't Users), so pass the name as a
    # label only.
    return reconcile_enrollment_authorization(
        enrollment, actor=None,
        actor_label=getattr(agent, "name", "") or "",
        note="Nutritionist approved",
    )


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

# Objective 3 / task 4.1: a governing meal/box authorization DENIED while the
# household is still only at Pending Verification (no service yet) removes the
# verification request -- the enrollment is DISREGARDED (dismissed), so the
# member leaves the Verification queue. Not auto-resumed: a later re-approval
# needs a fresh verification request (matches the manual disregard semantics).
_DENIAL_DISREGARD_NOTE = (
    "Auto-disregarded: internal-service meal/box authorization denied while the "
    "member was still awaiting verification. Verification request removed."
)

# Authorization statuses that mean "not yet approved -- awaiting a decision".
# A governing case in one of these states must NOT keep a household in service:
# only an approval (or Not Required) authorizes delivery. Because
# ``governing_case_key`` ranks an approval ABOVE a pending/never-requested case,
# a governing status in this set already means the client has NO approved
# internal-service authorization anywhere. EXPIRED is deliberately excluded --
# that is a post-approval terminal state handled by the delivery-window logic.
# NEVER_REQUESTED is deliberately excluded too: it is handled by the DENIAL
# full-stop (see _DENIED_EQUIVALENT_STATUSES), not this soft "waiting" downgrade.
# A truly blank status ("") stays here (data not yet imported -> benign wait).
_WAITING_AUTH_STATUSES = {
    ServiceAuthorizationStatus.PENDING,
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
            trigger="reconcile.awaiting_authorization",
        )
    except InvalidTransition:
        return enrollment
    try:
        truncate_future_deliveries(enrollment)
    except Exception:  # pragma: no cover - defensive
        pass
    return enrollment

# Stamped on the pause StageEvents raised by the case-CLOSURE full stop (distinct
# from the denial note above so the two rules stay independently auditable, and
# so the reactivation path can tell a closure hold from a denial hold).
_CLOSURE_HOLD_NOTE = "Auto-paused: last open internal-service meal/box case closed."


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
    """Reversibly stop service when the client's LAST open internal-service case
    closes, and park the member at the SERVICE_INACTIVE off-ramp.

    Steps: truncate future deliveries + pause (On Hold) every actionable
    governing enrollment + note the primary, THEN set the client's lifecycle
    stage to SERVICE_INACTIVE and emit a 'Service Inactive' timeline event on the
    transition IN.

    This is NOT a cancel -- the enrollments stay On Hold so a later open
    internal-service case can RESUME them (see the reactivation path in
    ``reconcile_internal_service_authorization``). Idempotent: once the household
    is On Hold and already SERVICE_INACTIVE, a re-import is a no-op (deduped note,
    single stage transition, create-once event). INELIGIBLE outranks
    SERVICE_INACTIVE and is never downgraded. Opens NO tickets -- visibility comes
    from StageEvents, the timeline, and the primary note.
    """
    from api.services import timeline
    from api.services.orders import truncate_future_deliveries

    result = {"paused": False, "cancelled": False, "service_inactive": False}
    actionable = [
        e
        for e in _governing_enrollments(client)
        if EnrollmentStage(e.stage) not in _TERMINAL_STAGES
        and EnrollmentStage(e.stage) != EnrollmentStage.SERVICE_COMPLETE
    ]

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
                    trigger="reconcile.governing_case_closed",
                )
                result["paused"] = True
            except InvalidTransition:
                pass

    if result["paused"]:
        _write_primary_system_note(
            client,
            (
                f"Service paused on {today}: the member's last open internal-service "
                f"case ({governing.case_id} - {label}) closed on {closed_on}. Future "
                f"deliveries were stopped and the household was placed On Hold. "
                f"Reversible -- a new open internal-service case resumes service."
            ),
            author_name=author,
        )

    # 2) Park at the reversible SERVICE_INACTIVE off-ramp. Set explicitly because
    #    derive_client_stage never PRODUCES this stage (it only keeps it sticky).
    #    Emit the timeline event only on the transition IN. INELIGIBLE outranks it.
    if client.lifecycle_stage not in (
        ClientStage.INELIGIBLE, ClientStage.SERVICE_INACTIVE,
    ):
        _set_client_stage(client, ClientStage.SERVICE_INACTIVE, actor=actor)
        timeline.event_for_member_service_inactive(
            client,
            case_id=governing.case_id,
            program=label,
            closed_on=closed_on,
            actor=author,
        )
        result["service_inactive"] = True
    return result


def _record_governing_case_change(client, governing, *, actor=None, actor_label=""):
    """Detect and record when the client's GOVERNING internal-service case
    changes (old -> new), writing a timeline event + a primary system note that
    describe the switch, its reason, the new authorization status and program.

    Idempotent two ways: the stored ``Client.governing_internal_case_id`` gates
    re-firing (updated to the settled governing case here), and the timeline
    event is create-once on the exact ``previous -> new`` pair. The FIRST
    governing case to land is recorded SILENTLY -- there is no prior case to
    switch from -- so this only speaks up on an actual change. Returns True when a
    change event was emitted. Best-effort."""
    new_id = str(governing.case_id) if governing is not None else ""
    old_id = client.governing_internal_case_id or ""
    if new_id == old_id:
        return False

    # Persist the settled pointer regardless (so the frontend can read the
    # program's governing case and the next reconcile compares against it).
    client.governing_internal_case_id = new_id
    client.save(update_fields=["governing_internal_case_id"])

    # First governing case (or all cases removed): not a SWITCH -- record silently.
    if not old_id or not new_id:
        return False

    from api.services import timeline

    old_case = next(
        (c for c in _internal_service_cases(client) if str(c.case_id) == old_id),
        None,
    )
    auth = getattr(governing, "service_authorization_status", "") or ""
    program = getattr(governing, "program_name", "") or "meal/box"
    old_auth = getattr(old_case, "service_authorization_status", "") if old_case else ""
    favorable = {
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    }
    if (
        old_case is not None
        and old_case.case_status in _CLOSED_CASE_STATUSES
        and governing.case_status not in _CLOSED_CASE_STATUSES
    ):
        reason = "the previous governing case closed"
    elif auth in favorable and old_auth not in favorable:
        reason = "a newer case was approved and now governs"
    else:
        reason = "a newer internal-service case now governs"

    author = actor_label or _actor_name(actor)
    _write_primary_system_note(
        client,
        (
            f"Governing internal-service case changed on "
            f"{timezone.localdate().isoformat()}: {old_id} \u2192 {new_id} "
            f"({program}, authorization: {auth or 'blank'}). Reason: {reason}."
        ),
        author_name=author,
    )
    timeline.event_for_member_governing_case_changed(
        client,
        previous_case_id=old_id,
        new_case_id=new_id,
        auth_status=auth,
        program=program,
        reason=reason,
        actor=author,
    )
    return True


# Stamped on the requeue StageEvent so it is clear WHY the household was moved
# back to Kitchen Assignment (a governing-case meals<->boxes product switch).
_PROGRAM_SWITCH_NOTE = (
    "Requeued for kitchen assignment: governing internal-service case switched "
    "product kind (meals<->boxes)."
)

# Post-verification stages a meals<->boxes switch requeues to a fresh Kitchen
# Assignment: these hold (or were queued for) a kitchen + calendar for the OLD
# product. VERIFIED (no kitchen/calendar yet) and pre-verification / terminal
# stages are left to the normal approval flow.
_PROGRAM_SWITCH_STAGES = {
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.ON_HOLD,
}


def requeue_enrollment_for_product_switch(
    enrollment, *, actor=None, actor_label="", note=_PROGRAM_SWITCH_NOTE,
):
    """Requeue ONE enrollment after a meals<->boxes product switch.

    Its current kitchen + delivery calendar are for the WRONG product, so:
      1. stop all future deliveries (shorten every plan window to yesterday so the
         nightly sync won't regenerate them);
      2. clear the kitchen + delivery cadence so a NEW kitchen assignment (kitchen
         + cadence + fresh calendar) is required for the new kind; and
      3. move the enrollment back to Kitchen Assignment (force, since
         Service Active has no direct edge back).

    Only acts on post-verification enrollments that actually hold a kitchen /
    calendar (``_PROGRAM_SWITCH_STAGES``); pre-verification / terminal stages are
    left to the normal approval flow. Best-effort; returns True when the
    enrollment was requeued. Shared by the governing-case-driven switch
    (:func:`_handle_program_switch`) and the manual Household-tab product-type
    correction (``MemberProductTypeView``).
    """
    from api.services.orders import truncate_future_deliveries

    if EnrollmentStage(enrollment.stage) not in _PROGRAM_SWITCH_STAGES:
        return False
    # 1) Stop the OLD product's future deliveries.
    try:
        truncate_future_deliveries(enrollment)
    except Exception:  # pragma: no cover - defensive
        pass
    # 2) Clear the kitchen + delivery cadence so a NEW kitchen assignment is
    #    required for the new kind.
    update_fields = []
    if enrollment.kitchen_id is not None:
        enrollment.kitchen = None
        update_fields.append("kitchen")
    if enrollment.delivery_weekdays:
        enrollment.delivery_weekdays = []
        update_fields.append("delivery_weekdays")
    if update_fields:
        enrollment.save(update_fields=update_fields)
    # 3) Requeue to Kitchen Assignment (force: Service Active has no direct edge
    #    back to Kitchen Assignment; On Hold does).
    if EnrollmentStage(enrollment.stage) != EnrollmentStage.KITCHEN_ASSIGNMENT:
        try:
            advance_enrollment(
                enrollment, EnrollmentStage.KITCHEN_ASSIGNMENT, actor=actor,
                actor_label=actor_label, note=note, force=True,
            )
        except InvalidTransition:
            pass
    return True


# Note stamped on the requeue when a mis-switched household (its kitchen can't
# make the governing kind, or its plan was built as the wrong product) is
# self-healed by the "Prepare Members for PO" sweep / remediation command.
_SWITCH_REMEDIATION_REQUEUE_NOTE = (
    "Requeued for kitchen assignment: delivery plan/kitchen didn't match the "
    "governing meal/box case after a product switch (auto-remediation)."
)


def classify_switched_household(enrollment):
    """Compare a household's delivery plan to its GOVERNING internal-service case
    kind and report how (if at all) it is mis-set after a meals<->boxes switch.

    Returns ``(gov_kind, action, reason)``:
      - ``action == "requeue"`` -- the assigned kitchen can't make the governing
        kind, OR the plan was BUILT as the wrong product; it needs a new kitchen
        + fresh calendar.
      - ``action == "rename"`` -- the plan / cadence / kitchen are already correct
        for the governing kind; only the ``program_name`` (which PO
        kind-resolution trusts first) is stale.
      - ``action is None`` -- consistent, nothing to do.

    Read-only. ``gov_kind`` is None when the governing kind can't be determined.
    """
    from api.models import KitchenProductType, ProductTypeKind
    from api.services.catalog import (
        detected_product_kind_for_enrollment,
        product_type_kind_for_name,
    )
    from api.services.orders import plan_built_kind

    gov = detected_product_kind_for_enrollment(enrollment)
    if gov is None:
        return None, None, None
    plan = enrollment.delivery_schedules.first()
    built = plan_built_kind(plan)
    name_kind = product_type_kind_for_name(enrollment.program_name)
    code = {
        ProductTypeKind.MEALS: KitchenProductType.MEAL,
        ProductTypeKind.BOXES: KitchenProductType.BOX,
    }
    k = enrollment.kitchen
    kitchen_ok = bool(k) and code.get(gov) in (getattr(k, "supported_products", None) or [])
    built_wrong = built is not None and built != gov
    if built_wrong or not kitchen_ok:
        return gov, "requeue", (
            "plan built as wrong kind" if built_wrong
            else "kitchen can't make governing kind"
        )
    if name_kind is not None and name_kind != gov:
        return gov, "rename", "stale program_name"
    return gov, None, None


def remediate_switched_household(
    enrollment, *, actor=None, actor_label="system:po-switch-remediation",
):
    """Fix a household mis-set after a meals<->boxes governing-case switch.

    Dispatches on :func:`classify_switched_household`:
      - ``requeue`` -> point ``product_type_override`` at the governing kind and
        requeue to Kitchen Assignment (compatible kitchen + fresh calendar).
      - ``rename``  -> set ``program_name`` to the governing program, reconcile
        the calendar (drop wrong-day leftovers, add the correct ones), and
        re-stamp the name on the scheduled rows.

    Returns the action taken (``"requeue"`` / ``"rename"``) or ``None``.
    Best-effort and idempotent -- safe to call on EVERY enrollment during the
    "Prepare Members for PO" sweep so any member's mismatch self-heals.
    """
    from django.utils import timezone

    from api.models import OrderSchedule, ProductType, ScheduleStatus
    from api.services.orders import sync_delivery_calendar

    gov, action, _reason = classify_switched_household(enrollment)
    if action == "requeue":
        pt = ProductType.objects.filter(type=gov).first()
        if pt is not None and enrollment.product_type_override_id != pt.pk:
            enrollment.product_type_override = pt
            try:
                enrollment.save(update_fields=["product_type_override"])
            except Exception:  # pragma: no cover - defensive
                pass
        did = requeue_enrollment_for_product_switch(
            enrollment, actor=actor, actor_label=actor_label,
            note=_SWITCH_REMEDIATION_REQUEUE_NOTE,
        )
        return "requeue" if did else None
    if action == "rename":
        gov_case = governing_internal_case(enrollment)
        name = getattr(gov_case, "program_name", "") or ""
        if not name:
            return None
        if enrollment.program_name != name:
            enrollment.program_name = name
            try:
                enrollment.save(update_fields=["program_name"])
            except Exception:  # pragma: no cover - defensive
                pass
        today = timezone.localdate()
        try:
            sync_delivery_calendar(enrollment, from_date=today)
        except Exception:  # pragma: no cover - defensive
            pass
        OrderSchedule.objects.filter(
            enrollment=enrollment, status=ScheduleStatus.SCHEDULED,
            anticipated_delivery_date__gte=today,
        ).update(program_name=name)
        return "rename"
    return None


def _served_product_kind(enrollment, old_case=None):
    """The Meals/Boxes kind the household is CURRENTLY SET UP FOR (its served
    kind), used as the baseline to detect a governing-case product switch.

    Robust to an empty governing pointer -- tries, in order: the explicit
    ``product_type_override`` (the verified kind), the product of an existing
    delivery schedule (what deliveries are actually built as), then the previous
    governing case's name-derived kind. Deliberately does NOT fall back to the
    new governing case (that would hide the switch). Returns a ProductTypeKind
    or None."""
    from api.models import ProductTypeKind
    from api.services.catalog import product_type_kind_for_name

    if enrollment is None:
        return None
    override = getattr(enrollment, "product_type_override", None)
    if override is not None:
        try:
            return ProductTypeKind(override.type)
        except ValueError:
            pass
    sched = enrollment.delivery_schedules.filter(
        product_type__isnull=False
    ).first()
    if sched is not None and sched.product_type:
        try:
            return ProductTypeKind(sched.product_type.type)
        except ValueError:
            pass
    if old_case is not None:
        return product_type_kind_for_name(old_case.program_name)
    return None


def _handle_program_switch(
    client, previous_governing_id, governing, *, actor=None, actor_label="",
):
    """AUTO-RECONCILE a governing-case Meals<->Boxes product switch at import
    time -- fully automatic.

    Detection is DATA-DRIVEN so it fires even when the stored governing pointer
    was never initialised: each governing enrollment's SERVED kind
    (:func:`_served_product_kind` -- override, then delivery schedule, then the
    previous governing case) is compared with the NEW governing case's DETECTED
    kind (name-derived). When they differ the switch is APPLIED automatically:

    * the enrollment ``product_type_override`` is pointed at the new kind so the
      served kind follows the governing case (and a re-import no-ops);
    * the household is requeued for a NEW kitchen assignment -- future deliveries
      stopped, kitchen + cadence cleared, moved to Kitchen Assignment
      (``requeue_enrollment_for_product_switch``); and
    * a 'Program Switched' timeline event + primary system note are written.

    Only a switch to an authorized, still-OPEN governing case requeues service (a
    closed / not-yet-approved successor is handled by the close/pull-back rules).
    Returns True when a switch was applied. Best-effort.
    """
    if governing is None:
        return False
    # Only a switch to an authorized, still-OPEN case requeues service (a closed
    # or not-yet-approved successor is handled by the close/pull-back rules).
    if governing.service_authorization_status not in (
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    ):
        return False
    if governing.case_status in _CLOSED_CASE_STATUSES:
        return False

    old_case = None
    if previous_governing_id:
        old_case = next(
            (c for c in _internal_service_cases(client)
             if str(c.case_id) == str(previous_governing_id)),
            None,
        )

    from api.models import ProductType, ProductTypeKind
    from api.services import timeline
    from api.services.catalog import (
        detected_product_kind_for_enrollment, product_type_kind_for_name,
    )

    # The governing case's DETECTED (name-derived) kind -- the new served kind.
    new_kind = product_type_kind_for_name(
        getattr(governing, "program_name", "")
    ) or product_type_kind_for_name(getattr(governing, "service_type", ""))
    if new_kind is None:
        return False
    new_product_type = ProductType.objects.filter(type=new_kind).first()

    switched = False
    old_kind = None
    for enr in _governing_enrollments(client):
        # The governing case's DETECTED kind (name-derived, ignores overrides).
        gov_kind = detected_product_kind_for_enrollment(enr) or new_kind
        served_kind = _served_product_kind(enr, old_case)
        # Not a resolvable meals<->boxes switch on this enrollment.
        if served_kind is None or served_kind == gov_kind:
            continue
        old_kind = served_kind
        # 1) Point the served kind at the governing case's kind so the Programs
        #    tab + resolvers follow it (and a re-import is idempotent). When no
        #    ProductType row exists for the new kind, clear the override so the
        #    served kind falls back to the (governing) derived kind instead.
        if new_product_type is not None:
            if enr.product_type_override_id != new_product_type.pk:
                enr.product_type_override = new_product_type
                try:
                    enr.save(update_fields=["product_type_override"])
                except Exception:  # pragma: no cover - defensive
                    pass
        elif enr.product_type_override_id is not None:
            enr.product_type_override = None
            try:
                enr.save(update_fields=["product_type_override"])
            except Exception:  # pragma: no cover - defensive
                pass
        # 2) Stop future deliveries, clear the kitchen + cadence, and requeue to
        #    Kitchen Assignment (the shared meals<->boxes requeue).
        if requeue_enrollment_for_product_switch(
            enr, actor=actor, actor_label=actor_label,
        ):
            switched = True

    if not switched or old_kind is None:
        return False

    old_label = ProductTypeKind(old_kind).label
    new_label = ProductTypeKind(new_kind).label
    reason = f"governing case switched from {old_label} to {new_label}"
    author = actor_label or _actor_name(actor)
    _write_primary_system_note(
        client,
        (
            f"Program switched on {timezone.localdate().isoformat()}: governing "
            f"internal-service case changed product from {old_label} to "
            f"{new_label} ({old_case.case_id} \u2192 {governing.case_id}). Future "
            f"deliveries were stopped and the household was requeued for a new "
            f"kitchen assignment (new kitchen, cadence and delivery calendar "
            f"required)."
        ),
        author_name=author,
    )
    timeline.event_for_member_program_switched(
        client,
        previous_kind=old_label,
        new_kind=new_label,
        previous_case_id=old_case.case_id,
        new_case_id=governing.case_id,
        auth_status=governing.service_authorization_status or "",
        reason=reason,
        actor=author,
    )
    # Also record the explicit product-type before -> after (meals<->boxes) on the
    # governing-case switch. De-duped on the case pair so re-running the reconcile
    # against an unchanged governing case never duplicates it.
    timeline.event_for_product_type_changed(
        enr,
        previous_label=old_label,
        new_label=new_label,
        actor=author,
        dedupe_key=(
            f"product_type_changed:{client.pk}:{old_case.case_id}:{governing.case_id}"
        ),
    )
    return True


# Stamped on the auto-pause of an additional (non-primary) member when the
# governing case switches Household -> Individual. Distinct from the manual /
# denial / closure holds so it stays independently auditable, and so the pause
# is recognisable as the pinned (pause_locked) one that only CS can lift.
_SCOPE_SWITCH_PAUSE_REASON = (
    "Auto-paused: governing internal-service case switched to Individual scope. "
    "This additional household member is pinned pending Customer Service review."
)


def _pause_lock_additional_members(client, primary, *, actor=None, actor_label=""):
    """Pause + PIN every ACTIVE additional (non-primary) member of the client's
    household when the governing case switches Household -> Individual.

    Sets each additional member's ``MemberDietaryProfile`` to PAUSED with
    ``pause_locked=True`` (so an agent cannot un-pause them from the Program tab),
    clears their kitchen meal result (excludes them from Purchase Orders), emits a
    'Member Paused' timeline event and writes a system note. Idempotent: a member
    already paused+locked is skipped. Returns the number of members newly pinned.
    Best-effort per member.
    """
    from api.models import MemberStatus, Note, NoteSource
    from api.services import timeline
    from api.services.orders import resync_scheduled_orders

    primary_id = getattr(primary, "pk", None)
    pinned = 0
    touched_enrollments = set()
    author = actor_label or _actor_name(actor)
    for enr in _governing_enrollments(client):
        for mv in enr.member_profiles.all():
            # Never pin the primary (they own the individual case).
            if mv.client_id and primary_id and str(mv.client_id) == str(primary_id):
                continue
            if mv.pause_locked and mv.status == MemberStatus.PAUSED:
                continue  # already pinned -- idempotent
            mv.status = MemberStatus.PAUSED
            mv.pause_locked = True
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            try:
                mv.save()
            except Exception:  # pragma: no cover - defensive
                continue
            pinned += 1
            touched_enrollments.add(enr.pk)
            try:
                timeline.event_for_member_paused(
                    mv, enrollment=enr,
                    reason=_SCOPE_SWITCH_PAUSE_REASON, actor=author,
                )
            except Exception:  # pragma: no cover - never break the reconcile
                pass
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.SYSTEM,
                        author_name=author, body=_SCOPE_SWITCH_PAUSE_REASON,
                    )
                except Exception:  # pragma: no cover - defensive
                    pass
    # Reflect the pauses on the future delivery calendar / Purchase Orders.
    for enr in _governing_enrollments(client):
        if enr.pk in touched_enrollments:
            try:
                resync_scheduled_orders(enrollment=enr)
            except Exception:  # pragma: no cover - defensive
                pass
    return pinned


# Reasons stamped on the members affected by a MANUAL (agent-initiated, from the
# Household tab) Household<->Individual scope switch. Like the AUTO import-
# reconcile pause (_SCOPE_SWITCH_PAUSE_REASON) this LOCKS (pause_locked) the
# members so an agent cannot un-pause them from the tab -- but unlike the CS-
# pinned auto pause (which only a Case Mismatch dismissal lifts), this lock is
# cleared automatically when the scope is corrected back to Household.
_MANUAL_SCOPE_TO_INDIVIDUAL_PAUSE_REASON = (
    "Auto-paused: program scope changed to Individual. Additional household "
    "members are paused and excluded from future deliveries, and are locked "
    "from being un-paused until the program scope is corrected back to Household."
)
_MANUAL_SCOPE_TO_HOUSEHOLD_RESUME_REASON = (
    "Auto-resumed: program scope changed to Household. Additional household "
    "members were un-paused and re-added to the delivery calendar."
)


def _pause_additional_members_manual(client, primary, *, actor=None, actor_label=""):
    """Manual Household -> Individual switch: PAUSE + LOCK every ACTIVE additional
    (non-primary) member and drop them from future deliveries.

    Sets ``pause_locked=True`` so an agent cannot un-pause the members from the
    Household tab while the program is Individual -- unlike the CS-pinned auto
    reconcile (:func:`_pause_lock_additional_members`, which only a Case Mismatch
    dismissal lifts), THIS lock is cleared automatically by
    :func:`_resume_additional_members_manual` when the scope is corrected back to
    Household. Clears each member's kitchen meal result (so they fall off Purchase
    Orders) and refreshes the future calendar. Returns the number newly paused.
    Best-effort per member.
    """
    from api.models import MemberStatus, Note, NoteSource
    from api.services import timeline
    from api.services.orders import resync_scheduled_orders

    primary_id = getattr(primary, "pk", None)
    paused = 0
    touched = set()
    author = actor_label or _actor_name(actor)
    for enr in _governing_enrollments(client):
        for mv in enr.member_profiles.all():
            if mv.client_id and primary_id and str(mv.client_id) == str(primary_id):
                continue  # never pause the primary
            if mv.status != MemberStatus.ACTIVE:
                continue  # already excluded (paused / out of orbit / inactive)
            mv.status = MemberStatus.PAUSED
            mv.pause_locked = True
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            try:
                mv.save()
            except Exception:  # pragma: no cover - defensive
                continue
            paused += 1
            touched.add(enr.pk)
            try:
                timeline.event_for_member_paused(
                    mv, enrollment=enr,
                    reason=_MANUAL_SCOPE_TO_INDIVIDUAL_PAUSE_REASON, actor=author,
                )
            except Exception:  # pragma: no cover
                pass
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.SYSTEM,
                        author_name=author,
                        body=_MANUAL_SCOPE_TO_INDIVIDUAL_PAUSE_REASON,
                    )
                except Exception:  # pragma: no cover
                    pass
    for enr in _governing_enrollments(client):
        if enr.pk in touched:
            try:
                resync_scheduled_orders(enrollment=enr)
            except Exception:  # pragma: no cover
                pass
    return paused


def _resume_additional_members_manual(client, primary, *, actor=None, actor_label=""):
    """Manual Individual -> Household switch: UN-PAUSE the additional (non-primary)
    members that were paused, then rebuild the delivery calendar so they receive
    service on the next Purchase Order on the household (primary) cadence.

    Clears the manual scope lock (``pause_locked``) that
    :func:`_pause_additional_members_manual` set, then re-runs the kitchen-aware
    meal rule per member (so they land Active or Out of Orbit as appropriate) and
    rebuilds each governing enrollment's calendar (missing plans + future
    occurrences). GENUINE CS pins are preserved: a locked member whose enrollment
    has an OPEN Case Mismatch flag is left paused+locked (only a CS dismissal may
    lift those). Returns the number un-paused. Best-effort per member.
    """
    from api.models import (
        CaseMismatchFlag, CaseMismatchStatus, MemberStatus, Note, NoteSource,
    )
    from api.services import timeline
    from api.services.meal_rules import reconcile_member_kitchen_output
    from api.services.orders import rebuild_delivery_calendar

    primary_id = getattr(primary, "pk", None)
    enrollments = list(_governing_enrollments(client))
    # Enrollments with an OPEN Case Mismatch flag: their locked members are
    # CS-pinned, NOT manual-scope-locked, so leave them untouched here.
    cs_pinned_enr_ids = set(
        CaseMismatchFlag.objects.filter(
            enrollment_id__in=[e.pk for e in enrollments],
            status=CaseMismatchStatus.OPEN,
        ).values_list("enrollment_id", flat=True)
    )
    resumed = 0
    touched = set()
    author = actor_label or _actor_name(actor)
    for enr in enrollments:
        for mv in enr.member_profiles.all():
            if mv.client_id and primary_id and str(mv.client_id) == str(primary_id):
                continue
            if mv.status != MemberStatus.PAUSED:
                continue
            # Leave genuine CS pins (open mismatch flag on this enrollment) for
            # Customer Service to lift; clear the manual scope lock otherwise.
            if mv.pause_locked and enr.pk in cs_pinned_enr_ids:
                continue
            try:
                mv.pause_locked = False
                # Re-run the meal rule against the household kitchen: sets status
                # (Active / Out of Orbit) + kitchen meal result. This IS the
                # explicit Individual->Household resume, so allow_resume=True lets
                # the meal rule move these members OFF the manual PAUSED status.
                reconcile_member_kitchen_output(
                    mv, enr.kitchen, save=False, allow_resume=True,
                )
                mv.save()
            except Exception:  # pragma: no cover - defensive
                continue
            resumed += 1
            touched.add(enr.pk)
            try:
                timeline.event_for_member_unpaused(
                    mv, enrollment=enr,
                    reason=_MANUAL_SCOPE_TO_HOUSEHOLD_RESUME_REASON, actor=author,
                )
            except Exception:  # pragma: no cover
                pass
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.SYSTEM,
                        author_name=author,
                        body=_MANUAL_SCOPE_TO_HOUSEHOLD_RESUME_REASON,
                    )
                except Exception:  # pragma: no cover
                    pass
    # Rebuild the calendar so resumed members get a plan + future occurrences on
    # the household (primary) cadence, ready for the next Purchase Order.
    for enr in _governing_enrollments(client):
        if enr.pk in touched:
            try:
                rebuild_delivery_calendar(enr)
            except Exception:  # pragma: no cover
                pass
    return resumed


def apply_manual_scope_switch_effects(client, new_scope, *, actor=None, actor_label=""):
    """Delivery/roster side effects of a MANUAL Household<->Individual scope
    switch made from the Household tab (see ``MemberHouseholdTypeView``):

    * **-> Individual**: pause every non-primary member and drop them from future
      deliveries.
    * **-> Household**: un-pause the (non-CS-pinned) additional members and
      rebuild the calendar so they get service on the next PO.

    Returns the count of members affected. Best-effort; never raises.
    """
    from api.models import CaseHouseholdType

    primary = _household_primary(client)
    if primary is None:
        return 0
    if new_scope == CaseHouseholdType.INDIVIDUAL:
        return _pause_additional_members_manual(
            client, primary, actor=actor, actor_label=actor_label,
        )
    if new_scope == CaseHouseholdType.HOUSEHOLD:
        return _resume_additional_members_manual(
            client, primary, actor=actor, actor_label=actor_label,
        )
    return 0


def dismiss_case_mismatch_flags_for_household(
    enrollment, *, dismissed_by="", reason="",
):
    """Dismiss any OPEN :class:`~api.models.CaseMismatchFlag` for this
    enrollment's household and clear the ``pause_locked`` CS pins on its members.

    Called when an agent RECONCILES the household scope from the Programs tab --
    that reconciliation IS the Customer Service review the flag was waiting on,
    so the flag clears from the Case Mismatch tab and the members are un-pinned.
    Mirrors :class:`CaseMismatchDismissView`. MUST run BEFORE the scope roster
    effects so an Individual->Household resume can un-pause the (now un-pinned)
    members. Returns the number of flags dismissed. Best-effort.
    """
    from django.db.models import Q

    from api.models import (
        CaseMismatchFlag, CaseMismatchStatus, MemberDietaryProfile,
    )

    if enrollment is None:
        return 0
    primary = _household_primary(enrollment.client)
    q = Q(enrollment=enrollment)
    if primary is not None:
        q |= Q(client=primary)
    flags = list(CaseMismatchFlag.objects.filter(q, status=CaseMismatchStatus.OPEN))
    now = timezone.now()
    for flag in flags:
        flag.status = CaseMismatchStatus.DISMISSED
        flag.dismissed_at = now
        flag.dismissed_by = dismissed_by or ""
        flag.dismiss_reason = reason or ""
        try:
            flag.save(update_fields=[
                "status", "dismissed_at", "dismissed_by", "dismiss_reason",
            ])
        except Exception:  # pragma: no cover - defensive
            continue
        # Clear the CS pins on the flag's household members so agents regain
        # control (mirrors the manual Case Mismatch dismissal).
        MemberDietaryProfile.objects.filter(
            enrollment_id=(flag.enrollment_id or enrollment.pk), pause_locked=True
        ).update(pause_locked=False)
    return len(flags)


def _ht_value(scope):
    """Coerce a household-type (CaseHouseholdType enum OR raw string) to its
    plain string value, or "" when empty. Lets scope logic compare an
    enrollment ``household_type_override`` (a CharField string) with a derived
    ``CaseHouseholdType`` uniformly."""
    return getattr(scope, "value", scope) or ""


def _handle_household_scope_switch(
    client, previous_governing_id, governing, *, actor=None, actor_label="",
):
    """AUTO-RECONCILE a governing-case Household<->Individual SCOPE switch at
    import time -- fully automatic, no Customer Service step.

    Detection is DATA-DRIVEN so it fires even when the stored governing pointer
    was never initialised: the NEW governing case's scope (derived LIVE from its
    program name) is compared with the household's CURRENTLY-SERVED scope -- the
    enrollment ``household_type_override`` (the verified scope shown on the
    Programs tab), falling back to the PREVIOUS governing case's derived scope.

    When they differ the switch is APPLIED automatically:

    * the new scope is written onto every governing enrollment's
      ``household_type_override`` so the Programs tab reflects it immediately;
    * any lingering OPEN Case Mismatch flag is auto-dismissed (+ CS pins cleared);
    * roster/delivery effects run (``apply_manual_scope_switch_effects``):
        - **Household -> Individual**: additional members auto-paused + pinned
          (the pin clears automatically when scope returns to Household);
        - **Individual -> Household**: additional members resumed + un-pinned and
          the delivery calendar rebuilt;
    * an audit 'Case Scope Reconciled' timeline event + primary system note are
      written.

    NO CaseMismatchFlag is created -- the fix is automatic. Idempotent: once the
    override matches the governing scope a re-import no-ops. Returns True when a
    switch was applied. Best-effort.
    """
    if governing is None:
        return False

    from api.models import CaseHouseholdType
    from api.serializers import derive_household_type

    primary = _household_primary(client)
    if primary is None:
        return False
    enrollments = _governing_enrollments(client)
    if not enrollments:
        return False

    new_ht = _ht_value(
        derive_household_type(None, getattr(governing, "program_name", ""))
    )
    if not new_ht:
        return False

    # CURRENTLY-SERVED (previous) scope baseline. Enrollments can carry DIFFERENT
    # overrides (e.g. a service-active one already on the new scope while a
    # pending-verification sibling still holds the old), so the baseline must be
    # an override that actually DIVERGES from the governing scope -- taking the
    # first override blindly would let a matching sibling mask a divergent one and
    # skip the fix. Fall back to the previous governing case's derived scope only
    # when NO enrollment carries an override at all.
    divergent = next(
        (
            e for e in enrollments
            if e.household_type_override
            and _ht_value(e.household_type_override) != new_ht
        ),
        None,
    )
    served = _ht_value(divergent.household_type_override) if divergent else ""
    # Pre-settle pointer, ONLY when it genuinely names a different case than the
    # (already-settled) new governing case -- otherwise it's useless as a "prev".
    old_case = None
    if previous_governing_id and str(previous_governing_id) != str(governing.case_id):
        old_case = next(
            (c for c in _internal_service_cases(client)
             if str(c.case_id) == str(previous_governing_id)),
            None,
        )
    if (
        not served
        and not any(e.household_type_override for e in enrollments)
        and old_case is not None
    ):
        served = _ht_value(derive_household_type(None, old_case.program_name))

    # No baseline to diff -> every enrollment already matches the governing scope
    # (or a brand-new member simply follows it); nothing to auto-fix. Idempotent
    # no-op on re-import.
    if not served or served == new_ht:
        return False

    # Resolve the previous case for the audit record. Prefer a runner-up
    # internal-service case whose derived scope matches the SERVED (previous)
    # scope -- that's the case the household was actually being served under --
    # else the pre-settle pointer, else any runner-up, else the linked case.
    if old_case is None or str(old_case.case_id) == str(governing.case_id):
        scoped = [
            c for c in _internal_service_cases(client)
            if str(c.case_id) != str(governing.case_id)
            and _ht_value(derive_household_type(None, c.program_name)) == served
        ]
        others = [
            c for c in _internal_service_cases(client)
            if str(c.case_id) != str(governing.case_id)
        ]
        pool = scoped or others
        old_case = max(pool, key=governing_case_key) if pool else None
    prev_case_id = (
        str(old_case.case_id) if old_case is not None
        else (str(enrollments[0].case_id) if enrollments[0].case_id else "")
    )

    author = actor_label or _actor_name(actor)

    # 1) Persist the new served scope on every governing enrollment so the
    #    Programs tab reflects the switch immediately.
    for e in enrollments:
        if _ht_value(e.household_type_override) != new_ht:
            e.household_type_override = new_ht
            try:
                e.save(update_fields=["household_type_override"])
            except Exception:  # pragma: no cover - defensive
                pass

    # 2) Auto-resolve any lingering OPEN Case Mismatch flag + clear CS pins BEFORE
    #    the roster effects (so an Individual->Household resume can un-pause the
    #    now-unpinned members). No NEW flag is ever created.
    for e in enrollments:
        try:
            dismiss_case_mismatch_flags_for_household(
                e, dismissed_by=author,
                reason="Auto-reconciled by import: governing case scope switch.",
            )
        except Exception:  # pragma: no cover - defensive
            pass

    # 3) Roster + delivery side effects: -> Individual pauses + PINS additional
    #    members (auto, cleared on return to Household); -> Household resumes +
    #    un-pins them and rebuilds the calendar.
    try:
        apply_manual_scope_switch_effects(
            client, new_ht, actor=actor, actor_label=actor_label,
        )
    except Exception:  # pragma: no cover - never break the reconcile
        pass

    # 4) Audit: primary system note + 'Case Scope Reconciled' timeline event.
    #    NO flag.
    from api.models import CaseMismatchType
    from api.services import timeline

    if (
        served == CaseHouseholdType.HOUSEHOLD
        and new_ht == CaseHouseholdType.INDIVIDUAL
    ):
        mismatch_type = CaseMismatchType.HOUSEHOLD_TO_INDIVIDUAL
        detail = (
            "governing internal-service case switched to Individual scope; the "
            "additional household members were automatically paused and pinned"
        )
    else:
        mismatch_type = CaseMismatchType.INDIVIDUAL_TO_HOUSEHOLD
        detail = (
            "governing internal-service case switched to Household scope; the "
            "additional household members were automatically resumed"
        )
    reason = (
        f"governing case scope changed from "
        f"{CaseHouseholdType(served).label} to {CaseHouseholdType(new_ht).label}"
    )
    _write_primary_system_note(
        client,
        (
            f"Household scope auto-reconciled on "
            f"{timezone.localdate().isoformat()}: {detail} "
            f"({prev_case_id or '\u2014'} \u2192 {governing.case_id})."
        ),
        author_name=author,
    )
    timeline.event_for_member_case_mismatch(
        client,
        mismatch_type=mismatch_type,
        previous_case_id=prev_case_id,
        new_case_id=governing.case_id,
        previous_household_type=served,
        new_household_type=new_ht,
        reason=reason,
        actor=author,
        auto_resolved=True,
    )
    return True


def _carry_verification_fields(target, source):
    """Carry the verified capture from ``source`` onto ``target`` after a
    governing-case replacement reused a pre-existing enrollment.

    Two independent parts:
      1. The captured DELIVERY ADDRESS + household size -- carried whenever the
         survivor lacks them, REGARDLESS of verification state (a verified
         enrollment can still be missing the address if an earlier carry copied
         the verified flag but not the FK).
      2. The verification FACT (who/when completed + requested + the verified
         flags) -- carried only when the survivor isn't itself verified (its own
         verification wins).

    Only fills empties; never overwrites. Returns the number of fields changed.
    Best-effort."""
    if source is None or target is None:
        return 0

    # (1) Captured delivery address + household size (ungated by verification).
    addr_fields = []
    if target.delivery_address_id is None and source.delivery_address_id is not None:
        target.delivery_address_id = source.delivery_address_id
        addr_fields.append("delivery_address")
    if target.household_size is None and source.household_size is not None:
        target.household_size = source.household_size
        addr_fields.append("household_size")
    if addr_fields:
        try:
            target.save(update_fields=addr_fields)
        except Exception:  # pragma: no cover - defensive
            addr_fields = []

    # (1b) Nutritionist sign-off -- carry it forward when the survivor lacks its
    # own (a governing-case switch doesn't change the clinical picture, so the
    # approval must not be dropped). Ungated by verification, like the address.
    if target.nutritionist_approved_at is None and source.nutritionist_approved_at is not None:
        target.nutritionist_approved_at = source.nutritionist_approved_at
        target.nutritionist_approved_by = source.nutritionist_approved_by
        target.nutritionist_signature = source.nutritionist_signature
        target.nutritionist_signature_image = source.nutritionist_signature_image
        target.nutritionist_approval_pdf_key = source.nutritionist_approval_pdf_key
        try:
            target.save(update_fields=[
                "nutritionist_approved_at", "nutritionist_approved_by",
                "nutritionist_signature", "nutritionist_signature_image",
                "nutritionist_approval_pdf_key",
            ])
        except Exception:  # pragma: no cover - defensive
            pass

    # (2) Verification fact -- only when the survivor isn't already verified.
    if target.verified_at is not None or source.verified_at is None:
        return len(addr_fields)
    fields = ["verified_at", "verified_by"]
    target.verified_at = source.verified_at
    target.verified_by = source.verified_by
    if target.requested_at is None and source.requested_at is not None:
        target.requested_at = source.requested_at
        fields.append("requested_at")
    if target.requested_by_id is None and source.requested_by_id is not None:
        target.requested_by = source.requested_by
        fields.append("requested_by")
    for flag in ("delivery_address_verified", "is_family_verified", "medicaid_type_verified"):
        if not getattr(target, flag, False) and getattr(source, flag, False):
            setattr(target, flag, True)
            fields.append(flag)
    try:
        target.save(update_fields=fields)
    except Exception:  # pragma: no cover - defensive
        fields = []
    return len(addr_fields) + len(fields)


_COND_SENTINEL = ["No Restriction"]
_MEDS_SENTINEL = ["No Medications"]


def _carry_dietary_profiles(target, source, *, overwrite=False):
    """Carry dietary fields onto ``target``'s member profiles from ``source``'s
    matching (by client) profiles.

    A household is verified once, capturing each member's menu type / allergies /
    restrictions. When a governing-case replacement REUSES a pre-existing
    enrollment, those profiles may be placeholders -- so copy the verified dietary
    config forward (the create-new path already does).

    ``overwrite=False`` (default): only fills EMPTIES; never clobbers data the
    survivor already carries. ``overwrite=True``: the SOURCE wins even over a
    non-blank target value -- used when the survivor was an unverified PLACEHOLDER
    whose menu/allergies are defaults, so the VERIFIED source is authoritative and
    a case switch can't silently reset e.g. Halal -> Standard. Returns the number
    of member profiles changed. Best-effort."""
    if target is None or source is None:
        return 0
    src_by_client = {
        p.client_id: p for p in source.member_profiles.all() if p.client_id
    }
    if not src_by_client:
        return 0
    changed = 0
    for tp in target.member_profiles.all():
        sp = src_by_client.get(tp.client_id)
        if sp is None:
            continue
        fields = []

        def take_str(f):
            sval = getattr(sp, f) or ""
            if not sval.strip():
                return
            if (overwrite or not (getattr(tp, f) or "").strip()) and getattr(tp, f) != sval:
                setattr(tp, f, sval); fields.append(f)

        def take_list(f):
            sval = getattr(sp, f)
            if not sval:
                return
            if (overwrite or not getattr(tp, f)) and getattr(tp, f) != sval:
                setattr(tp, f, sval); fields.append(f)

        # Menu / allergies / restrictions + captured meal config.
        take_str("menu_type")
        take_list("food_allergies")
        take_list("dietary_restrictions")
        take_str("other_dietary_restrictions")
        take_str("meal_category")
        if sp.meals_per_delivery is not None and (
            overwrite or tp.meals_per_delivery is None
        ) and tp.meals_per_delivery != sp.meals_per_delivery:
            tp.meals_per_delivery = sp.meals_per_delivery
            fields.append("meals_per_delivery")
        take_str("general_verification_notes")
        # Clinical / nutrition intake captured at verification. ``conditions`` /
        # ``medications`` default to sentinel single-item lists; treat those as
        # blank so real captured data still copies over.
        if (sp.conditions and sp.conditions != _COND_SENTINEL) and (
            overwrite or not tp.conditions or tp.conditions == _COND_SENTINEL
        ) and tp.conditions != sp.conditions:
            tp.conditions = sp.conditions; fields.append("conditions")
        if (sp.medications and sp.medications != _MEDS_SENTINEL) and (
            overwrite or not tp.medications or tp.medications == _MEDS_SENTINEL
        ) and tp.medications != sp.medications:
            tp.medications = sp.medications; fields.append("medications")
        for f in ("weight", "height", "medical_diet_details", "meal_plan",
                  "meal_plan_other", "assessment_notes", "nutritionist_pdf_key"):
            take_str(f)
        if sp.on_medical_diet and not tp.on_medical_diet:
            tp.on_medical_diet = True; fields.append("on_medical_diet")
        if sp.weeks_gestation is not None and (
            overwrite or tp.weeks_gestation is None
        ) and tp.weeks_gestation != sp.weeks_gestation:
            tp.weeks_gestation = sp.weeks_gestation; fields.append("weeks_gestation")
        if sp.months_postpartum is not None and (
            overwrite or tp.months_postpartum is None
        ) and tp.months_postpartum != sp.months_postpartum:
            tp.months_postpartum = sp.months_postpartum; fields.append("months_postpartum")
        if fields:
            try:
                tp.save(update_fields=fields)
                changed += 1
            except Exception:  # pragma: no cover - defensive
                pass
    return changed


# Fields copied when CREATING a carried member profile on a reused enrollment --
# mirrors the fresh-fork copy in ``replace_enrollment_for_case_change`` so both
# replacement paths carry a member's full dietary + clinical picture (and status)
# forward. The kitchen-rule RESULT (kitchen_meal_type/_food_notes) is recomputed
# against the surviving enrollment's kitchen, so it is intentionally NOT copied.
_CARRY_PROFILE_FIELDS = (
    "member_name", "dietary_restrictions", "food_allergies",
    "other_dietary_restrictions", "meal_category", "menu_type",
    "status", "meals_per_delivery", "general_verification_notes",
    "pause_locked", "eligibility_paused", "mobile_number",
    "conditions", "weeks_gestation", "months_postpartum", "medications",
    "weight", "height", "on_medical_diet", "medical_diet_details",
    "meal_plan", "meal_plan_other", "assessment_notes", "nutritionist_pdf_key",
)


def _create_missing_carried_profiles(target, source):
    """Create member profiles on ``target`` for every ``source`` member that has
    no profile there yet -- CONSERVING the member's prior status.

    Root-cause fix for stranded dependents: the reuse-existing-enrollment
    replacement path (:func:`_close_old_and_link_to_existing`) previously only
    filled BLANKS on profiles that already existed on the reused enrollment
    (:func:`_carry_dietary_profiles`). A household DEPENDENT present on the
    closed enrollment but missing from the reused survivor was never created --
    so they had no profile, no delivery plan and silently fell off the calendar
    while the primary kept being served. The fresh-CREATE replacement path
    already copies every member; this brings the reuse path to parity.

    The member's PRIOR status is preserved (an Active member stays Active so the
    downstream kitchen reconcile can plan them; a Paused / Inactive / Out-of-Range
    member carries that status verbatim and is not revived). Returns the number
    of profiles created. Best-effort."""
    from api.models import MemberDietaryProfile, MemberStatus

    if target is None or source is None:
        return 0
    existing = {p.client_id for p in target.member_profiles.all() if p.client_id}
    created = 0
    for sp in source.member_profiles.all():
        if not sp.client_id or sp.client_id in existing:
            continue
        # A REMOVED profile is a split-out dependent kept only as history; never
        # copy them forward (e.g. onto a reauthorization enrollment).
        if sp.status == MemberStatus.REMOVED:
            continue
        carried = {f: getattr(sp, f) for f in _CARRY_PROFILE_FIELDS}
        try:
            MemberDietaryProfile.objects.create(
                enrollment=target, client_id=sp.client_id,
                kitchen_meal_type="", kitchen_food_notes="", **carried,
            )
            created += 1
        except Exception:  # pragma: no cover - defensive
            pass
    return created


def _force_close_enrollment(enr):
    """Terminate ``enr`` as CLOSED even when the transition map has no edge from
    its current stage (e.g. verified / kitchen_assignment). Used ONLY for the
    SUPERSEDED enrollment in a governing-case replacement -- the replacement makes
    it read-only history regardless of stage, and a swallowed InvalidTransition
    previously left it LIVE (a duplicate). Bypasses the map deliberately; also
    truncates its future deliveries so the dead row can't keep a calendar."""
    now = timezone.now()
    if EnrollmentStage(enr.stage) in _TERMINAL_STAGES:
        return
    enr.stage = EnrollmentStage.CLOSED
    enr.stage_at = now
    if enr.closed_at is None:
        enr.closed_at = now
    try:
        enr.save(update_fields=["stage", "stage_at", "closed_at"])
    except Exception:  # pragma: no cover - defensive
        return
    try:
        from api.services.orders import truncate_future_deliveries

        truncate_future_deliveries(enr)
    except Exception:  # pragma: no cover - defensive
        pass


def _close_old_and_link_to_existing(
    live, existing, new_governing_case, actor=None, actor_label="", note="",
):
    """Close the current live enrollment and point an existing replacement
    enrollment at it. Used when the new governing case already has a live
    enrollment (e.g. a pending verification created earlier)."""
    from api.models import EnrollmentStage

    old_case_id = str(live.case.case_id) if live.case else (live.case_id or "")
    new_case_id = str(new_governing_case.case_id)
    # Capture serving state BEFORE the close (the carry reads this, not the
    # post-close CLOSED stage).
    live_was_serving = EnrollmentStage(live.stage) in _PRIOR_SERVING_STAGES
    live_was_paused = EnrollmentStage(live.stage) == EnrollmentStage.ON_HOLD

    with transaction.atomic():
        try:
            truncate_future_deliveries(live)
        except Exception:  # pragma: no cover - defensive
            pass

        try:
            advance_enrollment(
                live,
                EnrollmentStage.CLOSED,
                actor=actor,
                actor_label=actor_label,
                note=note or "Governing case replaced; enrollment closed as read-only history.",
                force=True,
                trigger="case_replaced",
            )
        except InvalidTransition:
            # No map edge to CLOSED from this stage -> close directly (the
            # replacement supersedes it regardless of stage).
            _force_close_enrollment(live)

        live.close_reason = "case_replaced"
        live.close_context = {
            "previous_case_id": old_case_id,
            "new_case_id": new_case_id,
            "new_enrollment_id": str(existing.pk),
        }
        try:
            live.save(update_fields=["close_reason", "close_context"])
        except Exception:  # pragma: no cover - defensive
            pass

        if existing.supersedes_id is None:
            existing.supersedes = live
            try:
                existing.save(update_fields=["supersedes"])
            except Exception:  # pragma: no cover - defensive
                pass

        # Carry the VERIFICATION FACT forward. A household is verified once; when
        # the governing case is replaced we keep the same verification instead of
        # forcing a re-verify. The other replacement path (a freshly CREATED
        # enrollment) already copies these; this path reused a pre-existing
        # (usually unverified Pending) enrollment, so copy them here too --
        # otherwise the surviving enrollment reaches Service Active with a BLANK
        # "Verified by" (the completer/date are lost). Only fill fields the
        # survivor doesn't already carry, so a genuine own verification wins.
        # Was the survivor a PLACEHOLDER (never verified on its own)? Captured
        # BEFORE _carry_verification_fields stamps verified_at. When it was, the
        # closed source (``live``) holds the REAL verified capture, so its dietary
        # config is authoritative and must OVERWRITE the placeholder's defaults --
        # otherwise a case switch silently resets e.g. Halal -> Standard (the menu
        # loss this fixes). A survivor with its OWN verification keeps its data.
        survivor_was_placeholder = existing.verified_at is None
        _carry_verification_fields(existing, live)
        # ...and the verified dietary config (menu/allergies/restrictions/clinical),
        # so a reused enrollment keeps the member's verified menu. Done BEFORE the
        # service carry so the meal-rule reconcile below sees the carried menu.
        _carry_dietary_profiles(existing, live, overwrite=survivor_was_placeholder)
        # CREATE any member profile missing entirely from the reused survivor (a
        # household DEPENDENT that lived only on the closed enrollment) conserving
        # their prior status -- otherwise they have no profile, no delivery plan
        # and silently fall off the calendar while the primary keeps serving. The
        # fresh-CREATE path already copies every member; this brings the reuse
        # path to parity. Done BEFORE the service carry so the promotion + calendar
        # rebuild below picks the new profiles up.
        _create_missing_carried_profiles(existing, live)

        # Carry the closed enrollment's service forward: if ``live`` was serving a
        # SAME-KIND program, drive ``existing`` back to Service Active with the
        # carried kitchen/cadence instead of leaving the household stranded at the
        # pre-existing (e.g. Pending Verification) stage and off the Purchase Order.
        from api.services.catalog import product_type_kind_for_name
        new_kind = (
            product_type_kind_for_name(new_governing_case.program_name or "")
            or product_type_kind_for_name(new_governing_case.service_type or "")
        )
        _carry_service_and_activate(
            existing, live, new_governing_case, new_kind,
            prior_was_serving=live_was_serving, prior_was_paused=live_was_paused,
            actor=actor, actor_label=actor_label,
        )


# Forward stage ladder used to walk a replacement enrollment up to Service
# Active. The transition map has NO direct pending_verification -> service_active
# edge, so a replacement of an already-serving member must step through each
# stage (each hop forced past its process gate).
_SERVICE_LADDER = [
    EnrollmentStage.PENDING_VALIDATION,
    EnrollmentStage.VALIDATED,
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
]
# Stages that mean the PRIOR enrollment was actually serving (or paused from
# serving) -- a replacement of one of these must land the new enrollment back in
# Service Active, not stranded at Pending Verification / Kitchen Assignment.
_PRIOR_SERVING_STAGES = frozenset({
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.ON_HOLD,
    EnrollmentStage.SERVICE_COMPLETE,
})


def _step_stage_forward(enrollment, target, *, actor, actor_label, note, trigger=""):
    """Advance ``enrollment`` FORWARD to ``target`` one legal hop at a time
    (forcing past process gates). on_hold resumes straight to the target.
    No-op if already at/after the target. Best-effort: stops at the first hop
    the map rejects rather than raising."""
    cur = EnrollmentStage(enrollment.stage)
    if cur == target:
        return
    if cur == EnrollmentStage.ON_HOLD:
        steps = [target]
    elif cur in _SERVICE_LADDER and target in _SERVICE_LADDER:
        ci, ti = _SERVICE_LADDER.index(cur), _SERVICE_LADDER.index(target)
        if ti <= ci:
            return
        steps = _SERVICE_LADDER[ci + 1:ti + 1]
    else:
        steps = [target]
    for nxt in steps:
        try:
            advance_enrollment(
                enrollment, nxt, actor=actor, actor_label=actor_label,
                note=note, force=True, trigger=trigger,
            )
        except InvalidTransition:
            break


NEED_REVIEW_TAG_NAME = "Need Review"


def _tag_client_need_review(client):
    """Attach the 'Need Review' client tag (idempotent) so an agent reviews an
    automated decision -- e.g. a paused household that received a new governing
    case (which must NOT silently resume service)."""
    if client is None:
        return
    try:
        from api.models import ClientTag

        tag, _ = ClientTag.objects.get_or_create(name=NEED_REVIEW_TAG_NAME)
        client.tags.add(tag)
    except Exception:  # pragma: no cover - defensive
        pass


def _carry_service_and_activate(
    new_enr, prior_enr, new_case, new_kind, *, prior_was_serving,
    prior_was_paused=False, actor, actor_label,
):
    """Carry the prior enrollment's kitchen/cadence onto ``new_enr`` and drive it
    to the right stage after a governing-case replacement.

    When the PRIOR enrollment was serving (Service Active / On Hold / Complete)
    and the product kind is UNCHANGED and its kitchen + cadence are known, the
    household must stay IN SERVICE: carry the kitchen/cadence, step ``new_enr`` up
    to Service Active, (re)create the delivery plan and rebuild the calendar.
    Otherwise the new enrollment advances only to Kitchen Assignment (a genuine
    meals<->boxes switch, or no prior kitchen/cadence -> a fresh kitchen
    assignment is required). Returns True when service was carried.
    """
    # HARD verification gate. A governing-case replacement must NOT promote a
    # household that was never verified: an approved NEW case does not skip
    # verification. When the surviving enrollment carries no verification fact
    # (``verified_at`` is blank) it stays at Pending Verification -- the earlier
    # ladder-forward would otherwise force it up to Kitchen Assignment (stamping
    # a VERIFIED it never had). Once a real verification completes,
    # ``reconcile_enrollment_authorization`` advances it normally.
    if not new_enr.verified_at:
        return False

    from api.services.catalog import product_type_kind_for_name
    from api.services.delivery import (
        create_member_delivery_schedules,
        current_household_cadence,
    )
    from api.services.meal_rules import reconcile_member_kitchen_output
    from api.services.orders import rebuild_delivery_calendar

    prior_kind = product_type_kind_for_name(prior_enr.program_name or "") or \
        product_type_kind_for_name(prior_enr.service_type or "")
    same_kind = (
        prior_kind is not None and new_kind is not None and prior_kind == new_kind
    )
    kitchen = prior_enr.kitchen
    cadence = current_household_cadence(prior_enr)

    carries = bool(prior_was_serving and same_kind and kitchen is not None and cadence)
    if not carries:
        # On a genuine product-KIND change (meals<->boxes), any kitchen +
        # delivery schedule carried onto the new enrollment is for the WRONG
        # product -- e.g. a Boxes kitchen on a Meals reauthorization. Clear it so
        # the agent must assign a valid kitchen for the new kind and a manual
        # calendar rebuild can't plan deliveries on an incompatible kitchen.
        kind_changed = (
            prior_kind is not None and new_kind is not None and prior_kind != new_kind
        )
        if kind_changed:
            if new_enr.kitchen_id:
                new_enr.kitchen = None
                new_enr.save(update_fields=["kitchen"])
            new_enr.delivery_schedules.all().delete()
        # No service to carry. Respect the NUTRITIONIST gate: a household that was
        # neither already serving NOR Nutritionist-approved rests at VERIFIED
        # (Pending Nutritionist) -- it must NOT be force-stepped past the sign-off
        # straight into Kitchen Assignment (which previously let a case switch skip
        # the nutrition review). An already-serving household (e.g. a meals<->boxes
        # requeue) has cleared nutrition, so it still goes to Kitchen Assignment.
        if new_enr.nutritionist_approved_at or prior_was_serving:
            target = EnrollmentStage.KITCHEN_ASSIGNMENT
            note = (
                "Verification (+ prior service/nutrition) carried from the previous "
                "enrollment; awaiting kitchen assignment for the new governing case."
            )
        else:
            target = EnrollmentStage.VERIFIED
            note = (
                "Verification carried from the previous enrollment; pending "
                "nutritionist review for the new governing case."
            )
        _step_stage_forward(
            new_enr, target, actor=actor, actor_label=actor_label,
            note=note, trigger="case_replaced",
        )
        # A manually/auto PAUSED (On Hold) household must NOT be silently taken
        # off hold by a new governing case just because there was no kitchen to
        # carry. Re-pause the new enrollment (drop future deliveries) and flag
        # Need Review so an agent decides whether to resume on the new case.
        if prior_was_paused:
            try:
                advance_enrollment(
                    new_enr, EnrollmentStage.ON_HOLD, actor=actor,
                    actor_label=actor_label, force=True, trigger="case_replaced",
                    note=("Kept On Hold: the prior household was paused; a new "
                          "governing case must not auto-resume service. Flagged "
                          "Need Review."),
                )
                from api.services.orders import truncate_future_deliveries

                truncate_future_deliveries(new_enr)
            except Exception:  # pragma: no cover - defensive
                pass
            _tag_client_need_review(new_enr.client)
        return False

    # Carry kitchen + weekdays.
    fields = []
    if new_enr.kitchen_id != kitchen.pk:
        new_enr.kitchen = kitchen
        fields.append("kitchen")
    if not new_enr.delivery_weekdays and prior_enr.delivery_weekdays:
        new_enr.delivery_weekdays = prior_enr.delivery_weekdays
        fields.append("delivery_weekdays")
    if fields:
        new_enr.save(update_fields=fields)

    # Return servable members to Active against the carried kitchen.
    from api.models import MemberStatus

    for mv in new_enr.member_profiles.all():
        if getattr(mv, "eligibility_paused", False):
            continue
        # A carried, still-serving household's members must be ACTIVE so they get
        # a delivery plan. A PENDING copy (the pre-service default) is EXCLUDED by
        # create_member_delivery_schedules (PENDING is in
        # SERVICE_EXCLUDED_MEMBER_STATUSES), so a served member would silently end
        # up with NO plan and NO cadence -- Service Active + a kitchen but nothing
        # to deliver. Promote PENDING -> ACTIVE before reconciling the kitchen
        # rule (which may still flip them Out-of-Orbit if unfulfillable).
        if mv.status == MemberStatus.PENDING:
            mv.status = MemberStatus.ACTIVE
            mv.save(update_fields=["status"])
        try:
            reconcile_member_kitchen_output(mv, kitchen=kitchen, allow_resume=True, save=True)
        except Exception:  # pragma: no cover - defensive
            pass

    _step_stage_forward(
        new_enr, EnrollmentStage.SERVICE_ACTIVE, actor=actor,
        actor_label=actor_label,
        note="Kitchen and cadence carried from the previous enrollment.",
        trigger="case_replaced",
    )

    # (Re)create the delivery plan + rebuild the calendar so the household stays
    # on the Purchase Order.
    try:
        from api.models import DeliveryCadence
        once_weekday = None
        if cadence == DeliveryCadence.ONCE_A_WEEK.value:
            wds = new_enr.delivery_weekdays or []
            once_weekday = wds[0] if wds else None
        create_member_delivery_schedules(
            new_enr, case=new_case, cadence=cadence, once_a_week_weekday=once_weekday,
            kitchen=kitchen, product_kind=new_kind,
        )
        rebuild_delivery_calendar(new_enr)
    except Exception:  # pragma: no cover - defensive
        pass

    # A PAUSED (On Hold) household must NOT be silently resumed by a new
    # governing case. The carry above rebuilt the serving enrollment (kitchen /
    # cadence / calendar preserved for a later manual resume); now re-pause it --
    # stop future deliveries so it drops off Purchase Orders -- and flag the
    # client Need Review so an agent decides whether to resume on the new case.
    if prior_was_paused:
        try:
            advance_enrollment(
                new_enr, EnrollmentStage.ON_HOLD, actor=actor,
                actor_label=actor_label, force=True, trigger="case_replaced",
                note=("Kept On Hold: the prior household was paused; a new "
                      "governing case must not auto-resume service. Flagged "
                      "Need Review."),
            )
            from api.services.orders import truncate_future_deliveries

            truncate_future_deliveries(new_enr)
        except Exception:  # pragma: no cover - defensive
            pass
        _tag_client_need_review(new_enr.client)
    return True


# A household that goes longer than this with NO open internal-service case must
# be RE-VERIFIED when a new case reopens service; within it, service resumes.
_REOPEN_REVERIFY_GAP_DAYS = 60

# Enrollment stages that mean the client still has a live enrollment (in the
# funnel or serving) -- if ANY exists, the normal resume/replace path owns it and
# the reopen below must NOT fire.
_LIVE_ENROLLMENT_STAGES = frozenset({
    EnrollmentStage.PENDING_VALIDATION,
    EnrollmentStage.VALIDATED,
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.ON_HOLD,
})
# Terminal stages a prior enrollment can rest in and still be a clone source.
_REOPEN_SOURCE_STAGES = frozenset({
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.SERVICE_COMPLETE,
})


def reopen_enrollment_for_new_case(client, new_governing_case, *, actor=None, actor_label=""):
    """Reopen service when a client whose ONLY internal-service enrollment is
    terminal gets a NEW open, approved governing case.

    Opens a fresh enrollment CLONED from the most-recent prior enrollment's data
    (roster + dietary/clinical intake, delivery address, verification facts,
    kitchen, cadence, nutritionist sign-off). The 60-day rule decides whether the
    household must be re-verified:

      * gap since the prior enrollment closed <= 60 days -> carry the verification
        (and nutritionist) forward and RESUME service (kitchen/cadence/calendar).
      * gap > 60 days -> DROP the carried verification so the new enrollment rests
        at Pending Verification: the household must be re-verified.

    Idempotent: once the live enrollment exists the normal resume/replace path
    takes over. Returns the new enrollment, or None when not applicable.
    """
    from api.models import (
        EnrollmentVerification, MemberDietaryProfile, MemberStatus,
    )
    from api.services.catalog import product_type_kind_for_name

    if new_governing_case is None:
        return None
    if new_governing_case.service_authorization_status not in (
        ServiceAuthorizationStatus.APPROVED, ServiceAuthorizationStatus.NOT_REQUIRED,
    ):
        return None
    if new_governing_case.case_status in _CLOSED_CASE_STATUSES:
        return None

    all_enr = list(EnrollmentVerification.objects.filter(client=client))
    # A live enrollment (funnel/serving) means the normal path handles it.
    if any(EnrollmentStage(e.stage) in _LIVE_ENROLLMENT_STAGES for e in all_enr):
        return None
    # Clone source: the most-recent VERIFIED terminal enrollment (has data).
    priors = [
        e for e in all_enr
        if e.verified_at and EnrollmentStage(e.stage) in _REOPEN_SOURCE_STAGES
    ]
    if not priors:
        return None
    prior = max(priors, key=lambda e: (e.closed_at or e.stage_at or e.opened_at))

    prior_close = prior.closed_at or prior.stage_at
    gap_days = (timezone.now() - prior_close).days if prior_close else 0
    reverify = gap_days > _REOPEN_REVERIFY_GAP_DAYS

    # GLOBAL safety: the per-case unique index covers every NON-terminal row
    # across ALL clients. If the governing case is already held by a live
    # enrollment (e.g. a relative/cross-client shared or mislinked case, like the
    # AKALLOO household), we must NOT fork a second live row onto it -- that both
    # violates the constraint and would double-claim someone else's case. Skip for
    # manual review. (Terminal rows -- incl. our own closed prior -- are exempt.)
    if EnrollmentVerification.objects.filter(case=new_governing_case).exclude(
        stage__in=[
            EnrollmentStage.CLOSED.value,
            EnrollmentStage.CANCELLED.value,
            EnrollmentStage.DISREGARDED.value,
        ]
    ).exists():
        return None

    new_kind = product_type_kind_for_name(new_governing_case.program_name or "") or \
        product_type_kind_for_name(new_governing_case.service_type or "")
    author = actor_label or _actor_name(actor)

    # The prior's case for the audit link: its own case if that differs from the
    # new one, else the case before it (same-case reopen). The closed prior can
    # keep pointing at the case -- the partial unique index exempts terminal rows.
    prev_case = (
        prior.case
        if (prior.case_id and str(prior.case_id) != str(new_governing_case.case_id))
        else prior.previous_case
    )

    with transaction.atomic():
        fields = {
            "client": prior.client,
            "household": prior.household,
            "case": new_governing_case,
            "previous_case": prev_case,
            "program_name": new_governing_case.program_name or "",
            "service_type": new_governing_case.service_type or "",
            "delivery_address": prior.delivery_address,
            "household_size": prior.household_size,
            "is_family_verified": prior.is_family_verified,
            "medicaid_type_verified": prior.medicaid_type_verified,
            "delivery_address_verified": prior.delivery_address_verified,
            "requested_by": prior.requested_by,
            "requested_at": timezone.now() if reverify else prior.requested_at,
            "supersedes": prior,
            "stage": EnrollmentStage.PENDING_VERIFICATION.value,
        }
        if not reverify:
            # Within 60 days: carry the verification + nutritionist sign-off so
            # _carry_service_and_activate can resume service without re-review.
            fields.update(
                verified_at=prior.verified_at,
                verified_by=prior.verified_by,
                nutritionist_approved_at=prior.nutritionist_approved_at,
                nutritionist_approved_by=prior.nutritionist_approved_by,
                nutritionist_signature=prior.nutritionist_signature,
                nutritionist_signature_image=prior.nutritionist_signature_image,
                nutritionist_approval_pdf_key=prior.nutritionist_approval_pdf_key,
            )
        new_enr = EnrollmentVerification.objects.create(**fields)

        # Clone the roster + full clinical/dietary intake (carried, not recollected).
        for mv in prior.member_profiles.all():
            MemberDietaryProfile.objects.create(
                enrollment=new_enr, client=mv.client, member_name=mv.member_name,
                dietary_restrictions=mv.dietary_restrictions,
                food_allergies=mv.food_allergies,
                other_dietary_restrictions=mv.other_dietary_restrictions,
                meal_category=mv.meal_category, menu_type=mv.menu_type,
                status=(MemberStatus.PENDING if reverify else mv.status),
                kitchen_meal_type="", kitchen_food_notes="",
                meals_per_delivery=mv.meals_per_delivery,
                general_verification_notes=mv.general_verification_notes,
                pause_locked=mv.pause_locked, mobile_number=mv.mobile_number,
                conditions=mv.conditions, weeks_gestation=mv.weeks_gestation,
                months_postpartum=mv.months_postpartum, medications=mv.medications,
                weight=mv.weight, height=mv.height,
                on_medical_diet=mv.on_medical_diet,
                medical_diet_details=mv.medical_diet_details,
                meal_plan=mv.meal_plan, meal_plan_other=mv.meal_plan_other,
                assessment_notes=mv.assessment_notes,
                nutritionist_pdf_key=mv.nutritionist_pdf_key,
            )

        day = timezone.localdate().isoformat()
        short = str(new_governing_case.case_id)[:8]
        if reverify:
            note = (
                f"Reopened on {day} for new open case {short}: the household went "
                f"{gap_days} days (> {_REOPEN_REVERIFY_GAP_DAYS}) with no open "
                "internal-service case, so re-verification is required. Roster + "
                "dietary data carried; service stays paused until re-verified."
            )
        else:
            _carry_service_and_activate(
                new_enr, prior, new_governing_case, new_kind,
                prior_was_serving=True, actor=actor, actor_label=actor_label,
            )
            note = (
                f"Reopened on {day} for new open case {short}: within "
                f"{_REOPEN_REVERIFY_GAP_DAYS} days ({gap_days}d) of the prior "
                "enrollment closing, so service resumed from the previous "
                "enrollment (no re-verification)."
            )
        _write_primary_system_note(client, note, author_name=author)

    return new_enr


def replace_enrollment_for_case_change(
    client, new_governing_case, *, actor=None, actor_label="",
):
    """Close the client's live enrollment and open a new one bound to
    ``new_governing_case`` whenever the governing internal-service case id
    changes. Carries over delivery address, kitchen/cadence (when compatible),
    and member profiles; applies scope-driven pause/enable; links old and new
    via supersession. Idempotent, transactional, best-effort.

    Returns the new EnrollmentVerification or None when no replacement happens.
    """
    from api.models import (
        CaseHouseholdType,
        EnrollmentStage,
        EnrollmentVerification,
        HouseholdMember,
        KitchenProductType,
        MemberDietaryProfile,
        ProductTypeKind,
        ServiceAuthorizationStatus,
    )
    from api.serializers import (
        derive_household_type,
        ensure_primary_of_own_household,
    )
    from api.services.catalog import product_type_kind_for_name
    from api.services.orders import (
        rebuild_delivery_calendar,
        truncate_future_deliveries,
    )

    if new_governing_case is None:
        return None

    if new_governing_case.service_authorization_status not in (
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    ):
        return None

    if new_governing_case.case_status in _CLOSED_CASE_STATUSES:
        return None

    terminal = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED}
    live = _primary_enrollment(client)
    if live is None:
        return None
    if EnrollmentStage(live.stage) in terminal:
        return None

    if str(live.case_id) == str(new_governing_case.case_id):
        return None

    # If the new governing case already has a live enrollment (e.g. a
    # pending-verification row created earlier), close the current live one
    # and use that existing row as the replacement rather than creating yet
    # another duplicate.
    existing = EnrollmentVerification.objects.filter(
        client=client, case=new_governing_case,
    ).exclude(pk=live.pk).exclude(stage__in=[s.value for s in terminal]).first()
    if existing is not None:
        _close_old_and_link_to_existing(live, existing, new_governing_case, actor, actor_label)
        return existing

    # An unbound enrollment (no case FK) is normally a first-time enrollment
    # waiting for its FIRST case to bind -- no replacement, the normal reconcile
    # attaches the governing case to it. BUT an enrollment that already served
    # and later lost its case FK (its governing case CLOSED and unbound it) must
    # be replaced when a genuinely NEW case arrives, so the old one is preserved
    # as read-only history and the new one supersedes it. We detect that by the
    # presence of a PRIOR internal-service case (distinct from the new governing
    # case) on the client -- the closed case the enrollment used to serve. This
    # also keeps a same-case denial->reapproval (no prior distinct case) from
    # spuriously forking a duplicate enrollment.
    if live.case is None:
        _served_stages = {
            EnrollmentStage.SERVICE_ACTIVE,
            EnrollmentStage.ON_HOLD,
            EnrollmentStage.SERVICE_COMPLETE,
        }
        # The PRIOR internal-service case this enrollment used to serve under (the
        # one that CLOSED and unbound it). Newest by governing-case tie-breaker.
        prior_case = (
            client.cases.filter(case_type=CaseType.INTERNAL_SERVICE)
            .exclude(case_id=new_governing_case.case_id)
            .order_by("-case_created_at", "-date_opened")
            .first()
        )
        if EnrollmentStage(live.stage) not in _served_stages or prior_case is None:
            return None
        # INVARIANT: every enrollment must reference its case. This served
        # enrollment lost its case FK when its governing case closed; RE-TIE it to
        # that prior (closed) case now -- BEFORE the fork below closes it as
        # read-only history -- so the superseded row is never left caseless.
        live.case = prior_case
        try:
            live.save(update_fields=["case"])
        except Exception:  # pragma: no cover - defensive
            pass

    old_case = live.case
    old_case_id = str(old_case.case_id) if old_case else ""
    new_case_id = str(new_governing_case.case_id)

    # Capture whether the OLD enrollment was actually serving BEFORE we close it
    # (the carry decision must not read the post-close CLOSED stage).
    live_was_serving = EnrollmentStage(live.stage) in _PRIOR_SERVING_STAGES
    live_was_paused = EnrollmentStage(live.stage) == EnrollmentStage.ON_HOLD

    old_kind = product_type_kind_for_name(live.program_name or "")
    if old_kind is None:
        old_kind = product_type_kind_for_name(live.service_type or "")

    new_kind = product_type_kind_for_name(new_governing_case.program_name or "")
    if new_kind is None:
        new_kind = product_type_kind_for_name(new_governing_case.service_type or "")

    old_scope = derive_household_type(
        None, old_case.program_name if old_case else live.program_name or ""
    )
    new_scope = derive_household_type(None, new_governing_case.program_name or "")

    author = actor_label or _actor_name(actor)

    # PRE-SERVICE REBIND (root-cause fix for the duplicate enrollments): a live
    # enrollment still IN THE FUNNEL -- never served (Pending Verification /
    # Verified / Kitchen Assignment) -- has no served history to preserve, so a
    # SAME-KIND governing-case-id change should simply REBIND the new case onto
    # it, NOT fork a parallel duplicate. Forking an in-funnel enrollment created
    # a second verification the nutritionist + logistics then worked
    # independently (two kitchen assignments for one household). A meals<->boxes
    # change still forks below (the verification/plan is for the wrong product);
    # a SERVED enrollment still forks (to keep the old one as read-only history).
    # Returning None lets the caller's normal path project the new case's
    # authorization onto the rebound enrollment, exactly as if it had been bound
    # all along -- and it's idempotent (next reconcile hits the same-case guard).
    same_kind = old_kind is not None and new_kind is not None and old_kind == new_kind
    if not live_was_serving and same_kind:
        try:
            live.case = new_governing_case
            live.program_name = new_governing_case.program_name or live.program_name
            live.service_type = new_governing_case.service_type or live.service_type
            live.save(update_fields=["case", "program_name", "service_type"])
        except Exception:  # pragma: no cover - defensive
            pass
        return None

    with transaction.atomic():
        # Close the old enrollment: stop delivery, move to CLOSED, save metadata.
        try:
            truncate_future_deliveries(live)
        except Exception:  # pragma: no cover - defensive
            pass

        try:
            advance_enrollment(
                live,
                EnrollmentStage.CLOSED,
                actor=actor,
                actor_label=actor_label,
                note="Governing case replaced; enrollment closed as read-only history.",
                force=True,
                trigger="case_replaced",
            )
        except InvalidTransition:
            # Some source stages (e.g. verified) have no map edge to CLOSED;
            # swallowing that previously left the old enrollment LIVE -> a
            # duplicate the nutritionist/kitchen flows could act on. Close it
            # directly so the superseded row always terminates.
            _force_close_enrollment(live)

        # Build the new enrollment with copied verification data.
        new_fields = {
            "client": live.client,
            "household": live.household,
            "case": new_governing_case,
            # Track the case this enrollment replaced (its predecessor's case) so
            # the prior-case link survives even after the old row is closed.
            "previous_case": old_case,
            "program_name": new_governing_case.program_name or "",
            "service_type": new_governing_case.service_type or "",
            "delivery_address": live.delivery_address,
            "household_size": live.household_size,
            "is_family_verified": live.is_family_verified,
            "medicaid_type_verified": live.medicaid_type_verified,
            "delivery_address_verified": live.delivery_address_verified,
            "verified_at": live.verified_at,
            "verified_by": live.verified_by,
            "requested_by": live.requested_by,
            "requested_at": live.requested_at,
            # Carry the Nutritionist legal sign-off forward: a governing-case
            # switch doesn't change the member's clinical picture, so it must not
            # silently drop the approval and force a needless re-review.
            "nutritionist_approved_at": live.nutritionist_approved_at,
            "nutritionist_approved_by": live.nutritionist_approved_by,
            "nutritionist_signature": live.nutritionist_signature,
            "nutritionist_signature_image": live.nutritionist_signature_image,
            "nutritionist_approval_pdf_key": live.nutritionist_approval_pdf_key,
            "supersedes": live,
            "stage": EnrollmentStage.PENDING_VERIFICATION.value,
        }

        # Kitchen/cadence carry rule (D4) is applied AFTER creation by
        # _carry_service_and_activate: the current kitchen + cadence carry over --
        # so the household stays IN SERVICE with a ready delivery calendar -- ONLY
        # when the PRIOR enrollment was serving AND the product kind is UNCHANGED
        # (meals->meals or boxes->boxes). A meals<->boxes switch always goes to
        # Kitchen Assignment (its plan is for the wrong product).
        new_enrollment = EnrollmentVerification.objects.create(**new_fields)

        # Record why the old one closed and the new one it was replaced by.
        live.close_reason = "case_replaced"
        live.close_context = {
            "previous_case_id": old_case_id,
            "new_case_id": new_case_id,
            "new_enrollment_id": str(new_enrollment.pk),
            "previous_product_kind": old_kind.value if old_kind else None,
            "new_product_kind": new_kind.value if new_kind else None,
            "previous_household_type": old_scope.value,
            "new_household_type": new_scope.value,
        }
        try:
            live.save(update_fields=["close_reason", "close_context"])
        except Exception:  # pragma: no cover - defensive
            pass

        # Copy member dietary profiles (D3) -- including the full clinical /
        # nutrition intake captured at verification (conditions, meds, weight /
        # height, medical diet, meal plan, assessment notes, gestation /
        # postpartum) so a governing-case switch never loses it. Only the
        # kitchen-rule RESULT (kitchen_meal_type / _food_notes) is cleared; it is
        # recomputed against the (possibly new) kitchen.
        for mv in live.member_profiles.all():
            MemberDietaryProfile.objects.create(
                enrollment=new_enrollment,
                client=mv.client,
                member_name=mv.member_name,
                dietary_restrictions=mv.dietary_restrictions,
                food_allergies=mv.food_allergies,
                other_dietary_restrictions=mv.other_dietary_restrictions,
                meal_category=mv.meal_category,
                menu_type=mv.menu_type,
                status=mv.status,
                kitchen_meal_type="",
                kitchen_food_notes="",
                meals_per_delivery=mv.meals_per_delivery,
                general_verification_notes=mv.general_verification_notes,
                pause_locked=mv.pause_locked,
                mobile_number=mv.mobile_number,
                # Clinical / nutrition intake (carried, not re-collected).
                conditions=mv.conditions,
                weeks_gestation=mv.weeks_gestation,
                months_postpartum=mv.months_postpartum,
                medications=mv.medications,
                weight=mv.weight,
                height=mv.height,
                on_medical_diet=mv.on_medical_diet,
                medical_diet_details=mv.medical_diet_details,
                meal_plan=mv.meal_plan,
                meal_plan_other=mv.meal_plan_other,
                assessment_notes=mv.assessment_notes,
                nutritionist_pdf_key=mv.nutritionist_pdf_key,
            )

        # Carry the prior kitchen/cadence and drive the new enrollment to the
        # right stage: an already-serving SAME-KIND member stays in Service Active
        # (kitchen + calendar carried); otherwise it lands in Kitchen Assignment.
        # This never strands a previously-serving member at Pending Verification.
        _carry_service_and_activate(
            new_enrollment, live, new_governing_case, new_kind,
            prior_was_serving=live_was_serving, prior_was_paused=live_was_paused,
            actor=actor, actor_label=actor_label,
        )

        # Apply scope effects to the new enrollment's copied member profiles (D3).
        try:
            apply_manual_scope_switch_effects(
                client, new_scope, actor=actor, actor_label=actor_label,
            )
        except Exception:  # pragma: no cover - defensive
            pass

        # Primary system note describing the replacement.
        em_dash = "\u2014"
        _write_primary_system_note(
            client,
            (
                f"Enrollment replaced on {timezone.localdate().isoformat()}: "
                f"closed {live.code or live.pk} "
                f"({old_case_id or em_dash}) and opened "
                f"{new_enrollment.code or new_enrollment.pk} ({new_case_id}) for "
                f"{(new_kind.label if new_kind else em_dash)} / {new_scope.label}."
            ),
            author_name=author,
        )

        # Re-anchor a DEPENDENT. A case switch forks the new enrollment onto the
        # relative's (shared) household while the member stays a non-primary
        # roster row -- leaving them "mis-anchored": their own verified/serving
        # enrollment living on someone else's household. Split them into their
        # OWN household now (the same end-state the verification wizard reaches
        # via split_dependent_into_own_enrollment). The verification / dietary /
        # clinical / kitchen / cadence / nutritionist / status carry above is
        # left UNTOUCHED; this only moves the household anchor (the client's
        # enrollments + their order schedules follow) and detaches the shared
        # roster row. No-op for a primary or household-less client.
        try:
            holder = new_enrollment.client
            membership = HouseholdMember.objects.filter(client=holder).first()
            if membership is not None and not membership.is_primary:
                ensure_primary_of_own_household(holder)
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger(__name__).exception(
                "dependent re-anchor after case replacement failed for %s",
                getattr(new_enrollment, "pk", None),
            )

        return new_enrollment


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
    _, end = gov.effective_authorization_window() if gov else (None, None)
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


def _resume_auto_paused_enrollment(
    enrollment,
    *,
    actor=None,
    hold_note=_DENIAL_HOLD_NOTE,
    resume_note="Auto-resumed: internal-service case re-approved.",
):
    """Resume an enrollment that THIS rule auto-paused (ON_HOLD) back to the
    stage it was held from. No-op when the most recent hold was NOT the matching
    auto-pause (so a manual Place-on-Hold -- or a hold from a DIFFERENT rule -- is
    never silently overridden).

    ``hold_note`` selects which auto-pause to reverse: the denial hold by default,
    or the closure hold (``_CLOSURE_HOLD_NOTE``) on the reactivation path."""
    # Only resume a household that is CURRENTLY on hold. Otherwise a STALE
    # historical auto-hold event lets a re-run (e.g. the eligibility reconcile,
    # which fires on every import) force an already-resumed, now SERVICE_ACTIVE
    # enrollment BACK DOWN to its old held-from stage -- regressing a serving
    # household to Verified/Kitchen Assignment and silently dropping it off POs.
    if EnrollmentStage(enrollment.stage) != EnrollmentStage.ON_HOLD:
        return enrollment
    last_hold = (
        StageEvent.objects.filter(
            enrollment=enrollment, to_stage=EnrollmentStage.ON_HOLD
        )
        .order_by("-entered_at")
        .first()
    )
    if not last_hold or not (last_hold.note or "").startswith(hold_note):
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
            note=resume_note,
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


# Objective 3 / task 4.3: a household still only at Kitchen Assignment
# (authorized, awaiting a kitchen -- never became an active member) whose
# governing internal-service case CLOSES or is DENIED is a hard off-ramp to
# INELIGIBLE. Distinct reasons so the timeline/note explain which trigger fired.
_KA_CLOSED_INELIGIBLE_REASON = (
    "the internal-service case closed while the household was still awaiting "
    "kitchen assignment"
)
_KA_DENIED_INELIGIBLE_REASON = (
    "the internal-service authorization was denied while the household was still "
    "awaiting kitchen assignment"
)


def _has_kitchen_assignment_enrollment(client):
    """True when any of the client's governing enrollments is at Kitchen
    Assignment (authorized, awaiting a manual kitchen assignment)."""
    return any(
        EnrollmentStage(e.stage) == EnrollmentStage.KITCHEN_ASSIGNMENT
        for e in _governing_enrollments(client)
    )


def _mark_kitchen_assignment_ineligible(client, *, reason, actor=None, actor_label=""):
    """Objective 3 / task 4.3: hard off-ramp a Kitchen-Assignment household to
    INELIGIBLE when its governing internal-service case closes or is denied.

    Sets the client's lifecycle stage to INELIGIBLE (sticky in
    ``derive_client_stage`` -- an agent must resolve it), emits a
    'Member marked Ineligible' timeline event + primary system note on the
    transition IN. Idempotent: a no-op when already INELIGIBLE. Returns True when
    newly set. The enrollment itself is paused (On Hold) by the caller's existing
    denial / closure handling; the CLIENT stage is the hard off-ramp here.
    """
    from api.services import timeline

    if client.lifecycle_stage == ClientStage.INELIGIBLE:
        return False
    author = actor_label or _actor_name(actor)
    _set_client_stage(client, ClientStage.INELIGIBLE, actor=actor)
    # Persist the reason so the Members list can display why (matches the
    # reconcile_client_eligibility path).
    if list(client.ineligible_reasons or []) != [reason]:
        client.ineligible_reasons = [reason]
        client.save(update_fields=["ineligible_reasons"])
    _write_primary_system_note(
        client,
        f"Marked Ineligible on {timezone.localdate().isoformat()}: {reason}.",
        author_name=author,
    )
    timeline.event_for_member_ineligible(client, reasons=[reason], actor=author)
    return True


def _bind_governing_case_to_serving_enrollment(client, governing):
    """Prevent (and self-heal) the 'serving enrollment left caseless' split.

    When a client has exactly ONE serving (SERVICE_ACTIVE/ON_HOLD) enrollment that
    is CASELESS while an OPEN governing internal-service case sits on a NON-serving
    stray (pending_verification/verified/kitchen_assignment) -- the per-case
    unique constraint blocks the normal reconcile from binding -- disregard the
    stray(s) holding that case and bind it onto the serving enrollment, so the
    member never delivers without a governing case (which breaks the auth/PO
    window). Skips when there are 0 or 2+ serving enrollments, when the serving
    one is already bound, or when the governing case is closed/cancelled. Runs on
    every case-save reconcile. Best-effort. Returns True when it bound."""
    if governing is None or governing.case_status in _CLOSED_CASE_STATUSES:
        return False
    from api.models import CaseType, EnrollmentVerification

    terminal = (
        EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED, EnrollmentStage.DISREGARDED,
    )
    serving_stages = (EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.ON_HOLD)
    live = [
        e for e in EnrollmentVerification.objects.filter(client=client)
        if EnrollmentStage(e.stage) not in terminal
    ]
    serving = [e for e in live if EnrollmentStage(e.stage) in serving_stages]
    if len(serving) != 1:
        return False
    serv = serving[0]
    if serv.case_id is not None and str(serv.case_id) == str(governing.case_id):
        return False  # already correctly bound
    if serv.case_id is not None:
        # The serving row is bound to a DIFFERENT case. Self-heal ONLY when that
        # case is a DEFERRED future case -- i.e. a mis-binding where a future
        # reauthorization/switch's FK landed on the actively-serving row (the prod
        # import bug). REPOINT it to the active governing case so service keeps
        # running on the right case, instead of letting the switch logic park or
        # close the serving member. A genuine change to a NON-deferred case is a
        # real switch and is left to the replace path.
        _cases = [c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE]
        if str(serv.case_id) not in {str(x) for x in deferred_extension_case_ids(_cases)}:
            return False  # a genuine case change is handled elsewhere
    # Holders across ALL clients (the per-case unique constraint is global): skip
    # if the case is held by a SERVING enrollment (ambiguous) OR by an enrollment
    # of a DIFFERENT client (cross-client mislink -- never steal another member's
    # enrollment; attempting the bind would raise IntegrityError and poison the
    # case-save transaction). Only free THIS client's non-serving strays.
    holders = list(
        EnrollmentVerification.objects.filter(case=governing)
        .exclude(pk=serv.pk)
        .exclude(stage__in=terminal)
    )
    if any(
        EnrollmentStage(e.stage) in serving_stages or e.client_id != serv.client_id
        for e in holders
    ):
        return False
    for e in holders:
        e.case = None
        e.stage = EnrollmentStage.DISREGARDED
        e.close_reason = "caseless_serving_fix"
        try:
            e.save(update_fields=["case", "stage", "close_reason"])
        except Exception:  # pragma: no cover - defensive
            pass
    serv.case = governing
    try:
        serv.save(update_fields=["case"])
    except Exception:  # pragma: no cover - defensive
        return False
    return True


REAUTH_ATTENTION_TAG_NAME = "Reauth Attention"
REAUTHORIZED_TAG_NAME = "Reauthorized"
NEED_ATTENTION_TAG_NAME = "Need Attention"
PENDING_NUTRITIONIST_TAG_NAME = "Pending Nutritionist"


def _need_attention_tag():
    """Get-or-create the orange "Need Attention" ClientTag -- flags a client for
    manual review (e.g. a dependent split out of a household while paused / out of
    orbit, so their preserved status needs a look)."""
    from api.models import ClientTag, ClientTagColor

    tag, _ = ClientTag.objects.get_or_create(
        name=NEED_ATTENTION_TAG_NAME,
        defaults={"color": ClientTagColor.ORANGE},
    )
    return tag


def _pending_nutritionist_tag():
    """Get-or-create the purple "Pending Nutritionist" ClientTag -- a client who
    was moved into their own case without a prior nutritionist approval and still
    needs review (surfaced on the Nutritionist section's pending page)."""
    from api.models import ClientTag, ClientTagColor

    tag, _ = ClientTag.objects.get_or_create(
        name=PENDING_NUTRITIONIST_TAG_NAME,
        defaults={"color": ClientTagColor.PURPLE},
    )
    return tag


def set_need_attention(client, needed):
    """Apply or clear the "Need Attention" tag on ``client``. Idempotent."""
    if client is None:
        return
    tag = _need_attention_tag()
    if needed:
        client.tags.add(tag)
    else:
        client.tags.remove(tag)


def set_pending_nutritionist(client, needed):
    """Apply or clear the "Pending Nutritionist" tag on ``client``. Idempotent."""
    if client is None:
        return
    tag = _pending_nutritionist_tag()
    if needed:
        client.tags.add(tag)
    else:
        client.tags.remove(tag)


def _reauth_attention_tag():
    """Get-or-create the red "Reauth Attention" ClientTag."""
    from api.models import ClientTag, ClientTagColor

    tag, _ = ClientTag.objects.get_or_create(
        name=REAUTH_ATTENTION_TAG_NAME,
        defaults={"color": ClientTagColor.RED},
    )
    return tag


def _reauthorized_tag():
    """Get-or-create the green "Reauthorized" ClientTag -- a positive indicator
    that a reauthorization is parked/scheduled for the client."""
    from api.models import ClientTag, ClientTagColor

    tag, _ = ClientTag.objects.get_or_create(
        name=REAUTHORIZED_TAG_NAME,
        defaults={"color": ClientTagColor.GREEN},
    )
    return tag


def set_reauth_attention(client, needed):
    """Apply or clear the "Reauth Attention" tag on ``client``. Idempotent."""
    if client is None:
        return
    tag = _reauth_attention_tag()
    if needed:
        client.tags.add(tag)
    else:
        client.tags.remove(tag)


def set_reauthorized(client, needed):
    """Apply or clear the green "Reauthorized" tag on ``client`` -- applied while a
    reauthorization is parked (scheduled), cleared once none remain. Idempotent."""
    if client is None:
        return
    tag = _reauthorized_tag()
    if needed:
        client.tags.add(tag)
    else:
        client.tags.remove(tag)


def split_dependent_into_own_enrollment(client, new_enrollment, *, actor=None, actor_label=""):
    """Split a household DEPENDENT (``client``) into their OWN internal-service
    case, called right after the verification wizard creates + verifies
    ``new_enrollment`` for a client who was a NON-primary member of a shared
    household.

    Steps (see docs/dependent_split_plan.md):
      * copy the primary's delivery address/notes + weekdays onto the new
        enrollment, and fill any per-member fields the wizard didn't carry from
        the member's old household profile;
      * move nutritionist data when the member was nutritionist-approved (treat
        the new enrollment as approved); else tag ``Pending Nutritionist``;
      * detach the member from the shared household roster and KEEP their old
        profile as ``REMOVED`` history (rebuild the old calendar so they drop
        off); make the client PRIMARY of their own household and re-home the new
        enrollment there;
      * preserve the member's prior service status: ACTIVE -> carry kitchen /
        cadence and activate; paused / out-of-orbit / inactive -> keep the status,
        carry the kitchen for continuity, and tag ``Need Attention``.

    No-op (returns ``{"split": False}``) when the client is not a splittable
    dependent (not in a household, or already primary)."""
    from api.models import (
        MEMBER_PAUSED_STATUSES, HouseholdMember, MemberDietaryProfile,
        MemberStatus, TimelineEventType,
    )
    from api.serializers import ensure_household_with_primary
    from api.services.catalog import product_kind_for_enrollment
    from api.services.orders import rebuild_delivery_calendar
    from api.services.timeline import emit_timeline_event

    if client is None or new_enrollment is None:
        return {"split": False}
    membership = (
        HouseholdMember.objects.filter(client=client).select_related("household").first()
    )
    if membership is None or membership.is_primary:
        return {"split": False}  # not a dependent -> nothing to split
    shared_household = membership.household

    old_profile = (
        MemberDietaryProfile.objects
        .filter(client=client, enrollment__household=shared_household)
        .exclude(enrollment=new_enrollment)
        .exclude(status=MemberStatus.REMOVED)
        .select_related("enrollment", "enrollment__delivery_address", "enrollment__kitchen")
        .order_by("-enrollment__opened_at")
        .first()
    )
    old_enr = old_profile.enrollment if old_profile else None
    # This enrollment is the DEPENDENT's alone. The verification wizard may have
    # seeded it with the whole shared household's participant rows (the extension
    # sends every household member); drop everyone but the dependent so the new
    # enrollment -- and the dependent's own household roster -- is just them.
    new_enrollment.member_profiles.exclude(client=client).delete()
    new_profile = new_enrollment.member_profiles.filter(client=client).first()
    # The caller may hand us a BARE enrollment with no member profile for the
    # dependent (e.g. the CRM "Request Verification" button, which creates only
    # the enrollment row -- unlike the verification wizard, which pre-seeds the
    # profile). Create it from the old household profile so the per-member carry,
    # status preservation, cadence/kitchen carry and activation below all run --
    # i.e. the CRM button does the exact same work as the extension/wizard path.
    if new_profile is None and old_profile is not None:
        new_profile = MemberDietaryProfile.objects.create(
            enrollment=new_enrollment,
            client=client,
            member_name=old_profile.member_name or "",
        )
    prior_status = old_profile.status if old_profile else MemberStatus.ACTIVE
    # Per-MEMBER nutritionist review: a dependent can live in a household that is
    # nutritionist-approved at the ENROLLMENT level while never having been
    # individually reviewed. Base the decision on THEIR own nutrition data (meal
    # plan / assessment notes / signed PDF), NOT the household's sign-off -- an
    # un-reviewed member must surface as Pending Nutritionist.
    nutritionist_approved = bool(
        old_profile and (
            (old_profile.nutritionist_pdf_key or "").strip()
            or (old_profile.meal_plan or "").strip()
            or (old_profile.assessment_notes or "").strip()
        )
    )

    # (1) Copy common (from primary) + per-member + nutritionist data.
    if old_enr is not None:
        # Carry the verification FACT (verified_at/by + flags), delivery address,
        # household size and nutritionist sign-off from the old household
        # enrollment so the dependent's new enrollment is already VERIFIED (no
        # re-verification -- plan decision #3). This also satisfies the
        # verified_at gate in _carry_service_and_activate below, without which the
        # kitchen/cadence carry + activation silently no-op.
        # Carry the household's verified fact + delivery address + nutritionist
        # sign-off forward so an ACTIVE dependent stays ACTIVE (service is not
        # interrupted). We do NOT clear the carried nutritionist approval for a
        # member who wasn't individually reviewed -- keeping their service status
        # active is what's wanted; the "needs a nutritionist look" signal is the
        # Pending Nutritionist TAG applied in step 5, not a status downgrade.
        _carry_verification_fields(new_enrollment, old_enr)
        enr_fields = []
        if old_enr.delivery_address_id and not new_enrollment.delivery_address_id:
            new_enrollment.delivery_address = old_enr.delivery_address
            enr_fields.append("delivery_address")
        if old_enr.delivery_weekdays and not new_enrollment.delivery_weekdays:
            new_enrollment.delivery_weekdays = old_enr.delivery_weekdays
            enr_fields.append("delivery_weekdays")
        if nutritionist_approved:
            for f in (
                "nutritionist_approved_at", "nutritionist_approved_by_id",
                "nutritionist_signature", "nutritionist_signature_image",
                "nutritionist_approval_pdf_key",
            ):
                setattr(new_enrollment, f, getattr(old_enr, f))
                enr_fields.append(f.removesuffix("_id"))
        if enr_fields:
            new_enrollment.save(update_fields=list(dict.fromkeys(enr_fields)))
        if new_profile is not None and old_profile is not None:
            # AUTHORITATIVE copy of the member's data from their old household
            # profile -- the whole point is to carry their prior verification
            # forward, NOT re-verify. Status + pause flags are handled separately;
            # kitchen_meal_type/_food_notes are recomputed against the new kitchen.
            copied = []
            for f in _CARRY_PROFILE_FIELDS:
                if f in ("status", "pause_locked", "eligibility_paused"):
                    continue
                setattr(new_profile, f, getattr(old_profile, f))
                copied.append(f)
            if copied:
                new_profile.save(update_fields=copied)

    # (2) Detach from the shared household; KEEP the old profile as REMOVED.
    if old_profile is not None:
        old_profile.status = MemberStatus.REMOVED
        old_profile.status_changed_at = timezone.now()
        old_profile.save(update_fields=["status", "status_changed_at"])
    membership.delete()
    if old_enr is not None:
        try:
            rebuild_delivery_calendar(old_enr)
        except Exception:  # pragma: no cover - defensive
            pass
        if old_enr.household_size and old_enr.household_size > 0:
            old_enr.household_size -= 1
            old_enr.save(update_fields=["household_size"])

    # (3) Make the client primary of their OWN household; re-home the new enrollment.
    own_household = ensure_household_with_primary(client)
    if new_enrollment.household_id != own_household.household_id:
        new_enrollment.household = own_household
        new_enrollment.save(update_fields=["household"])

    # (4) Preserve status + carry kitchen/cadence.
    if old_enr is not None and prior_status == MemberStatus.ACTIVE:
        _carry_service_and_activate(
            new_enrollment, old_enr, new_enrollment.case,
            product_kind_for_enrollment(new_enrollment),
            prior_was_serving=True, actor=actor, actor_label=actor_label,
        )
    else:
        if old_enr is not None and old_enr.kitchen_id and not new_enrollment.kitchen_id:
            new_enrollment.kitchen = old_enr.kitchen
            new_enrollment.save(update_fields=["kitchen"])
        if new_profile is not None and new_profile.status != prior_status:
            new_profile.status = prior_status
            new_profile.status_changed_at = timezone.now()
            new_profile.save(update_fields=["status", "status_changed_at"])

    # (5) Tags.
    if prior_status in MEMBER_PAUSED_STATUSES:
        set_need_attention(client, True)
    if not nutritionist_approved:
        set_pending_nutritionist(client, True)

    # (6) Timeline: removal on the old enrollment + arrival on the new one.
    member_name = (
        f"{client.first_name or ''} {client.last_name or ''}".strip()
        or (old_profile.member_name if old_profile else "")
        or str(client.pk)
    )
    if old_enr is not None:
        emit_timeline_event(
            client=old_enr.client, event_type=TimelineEventType.HOUSEHOLD_MEMBER_REMOVED,
            occurred_at=timezone.now(),
            title=f"Removed {member_name} from case",
            subtitle="Moved from this household to their own enrollment (data carried).",
            enrollment=old_enr, source="system", actor=actor_label or "",
            metadata={
                "removed_client_id": str(client.pk),
                "removed_member_name": member_name,
            },
        )
    emit_timeline_event(
        client=client, event_type=TimelineEventType.ENROLLED,
        occurred_at=timezone.now(), title="Split into own case",
        subtitle="Moved from a shared household into their own enrollment (data carried).",
        enrollment=new_enrollment, source="system", actor=actor_label or "",
    )
    return {
        "split": True,
        "old_enrollment_id": old_enr.pk if old_enr else None,
        "prior_status": prior_status,
        "nutritionist_approved": nutritionist_approved,
    }


def sync_reauthorized_tag(client):
    """Keep the "Reauthorized" tag in sync with whether the client currently has a
    parked (SCHEDULED_EXTENSION) reauthorization enrollment."""
    from api.models import EnrollmentStage, EnrollmentVerification

    if client is None:
        return
    has_parked = EnrollmentVerification.objects.filter(
        client=client, stage=EnrollmentStage.SCHEDULED_EXTENSION,
    ).exists()
    set_reauthorized(client, has_parked)


def _reauth_kind_scope_mismatch(cases):
    """True when an approved reauthorization (``is_extension``) case does NOT line
    up (same product kind + same scope) with any currently-served case -- a
    reauth that can't be auto-extended and needs a human (it will switch normally
    rather than defer). Requires a current served case to compare against."""
    favorable = {
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    }
    current = [
        c for c in cases
        if not getattr(c, "is_extension", False)
        and c.service_authorization_status in favorable
    ]
    if not current:
        return False
    for c in cases:
        if not getattr(c, "is_extension", False):
            continue
        if c.service_authorization_status not in favorable:
            continue
        kind_c = _case_product_kind(c)
        scope_c = c.household_type
        if not any(
            _case_product_kind(o) == kind_c and o.household_type == scope_c
            for o in current
        ):
            return True
    return False


def _record_reauth_scheduled_event(enrollment, *, actor=None, actor_label=""):
    """Log a 'Reauthorization scheduled' event on the enrollment's history (a
    StageEvent + the client timeline), so parking a reauth is auditable just like
    its later activation."""
    from api.models import StageEntityType, StageEvent, StageEventSource

    note = "Reauthorization scheduled; parked until its authorization window begins."
    is_system = actor is None and (
        not actor_label or actor_label.strip().lower().startswith("system:")
    )
    try:
        StageEvent.objects.create(
            entity_type=StageEntityType.ENROLLMENT,
            enrollment=enrollment,
            client=enrollment.client,
            from_stage="",
            to_stage=EnrollmentStage.SCHEDULED_EXTENSION.value,
            source=StageEventSource.AUTO if is_system else StageEventSource.MANUAL,
            actor=stage_event_actor(actor),
            note=note,
        )
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from api.models import TimelineEventType
        from api.services.timeline import emit_timeline_event

        emit_timeline_event(
            client=enrollment.client,
            event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED,
            occurred_at=timezone.now(),
            title="Reauthorization scheduled",
            subtitle=note,
            enrollment=enrollment,
            case=enrollment.case,
            source="system",
            actor=actor_label or "",
        )
    except Exception:  # pragma: no cover - defensive
        pass


def _carry_waiting_schedule(waiting, live, reauth):
    """Give a parked reauthorization enrollment a DISPLAY-only ``WAITING`` delivery
    schedule mirroring the live enrollment's kitchen + cadence, so the Program tab
    shows the planned kitchen/cadence/schedule. A ``WAITING`` schedule never
    generates Purchase Order occurrences (every occurrence/PO path filters
    ``status=SCHEDULED``); the real SCHEDULED plan + calendar are (re)built when
    the reauthorization activates. Best-effort; idempotent."""
    from api.models import ScheduleStatus
    from api.services.catalog import product_kind_for_enrollment
    from api.services.delivery import (
        create_member_delivery_schedules,
        current_household_cadence,
    )

    if waiting is None or live is None:
        return
    # Only mirror kitchen/cadence from a SAME-KIND live program -- a Boxes
    # kitchen must never be carried onto a Meals reauthorization (or vice versa).
    live_kind = product_kind_for_enrollment(live)
    reauth_kind = _case_product_kind(reauth) if reauth is not None else None
    if live_kind is not None and reauth_kind is not None and live_kind != reauth_kind:
        return
    if live.kitchen_id and waiting.kitchen_id != live.kitchen_id:
        waiting.kitchen = live.kitchen
        try:
            waiting.save(update_fields=["kitchen"])
        except Exception:  # pragma: no cover - defensive
            pass
    if waiting.delivery_schedules.exists():
        return
    cadence = current_household_cadence(live)
    if not (waiting.kitchen_id and cadence):
        return  # nothing to mirror (no kitchen/cadence on the live enrollment yet)
    try:
        create_member_delivery_schedules(
            waiting, case=reauth, cadence=cadence, kitchen=waiting.kitchen,
            product_kind=_case_product_kind(reauth),
            status=ScheduleStatus.WAITING,
        )
    except Exception:  # pragma: no cover - defensive
        pass


def _close_orphaned_scheduled_extensions(client, *, actor=None, actor_label=""):
    """Close any parked SCHEDULED_EXTENSION (reauthorization) enrollment whose
    case has since CLOSED/CANCELLED. The future reauth it was holding never
    happened (the case was closed), so the parked row must not linger looking like
    a pending extension. Returns the count closed."""
    from api.models import EnrollmentVerification, EnrollmentStage

    closed = 0
    for enr in EnrollmentVerification.objects.filter(
        client=client, stage=EnrollmentStage.SCHEDULED_EXTENSION.value,
    ).select_related("case"):
        if enr.case is None or enr.case.case_status not in _CLOSED_CASE_STATUSES:
            continue
        _force_close_enrollment(enr)
        try:
            enr.close_reason = "scheduled_extension_case_closed"
            enr.save(update_fields=["close_reason"])
        except Exception:  # pragma: no cover - defensive
            pass
        closed += 1
    return closed


def _park_deferred_extensions(client, cases, *, actor=None, actor_label=""):
    """Ensure each DEFERRED future reauthorization extension has a parked,
    NON-SERVING ``SCHEDULED_EXTENSION`` enrollment bound to its case.

    Carries the household's verified capture + member roster from the live
    serving enrollment so activation (later, at the window boundary -- see the
    reauthorization activation task) is a clean promotion. Idempotent: an
    already-parked case is left untouched. Does nothing when there's no live
    serving enrollment to extend from. The ``supersedes`` link to the serving
    enrollment is set at ACTIVATION, not here, so the serving row never reads as
    superseded while the extension is merely waiting.
    """
    from api.models import EnrollmentStage, EnrollmentVerification

    deferred = deferred_extension_case_ids(cases)
    if not deferred:
        return
    live = _primary_enrollment(client)
    if live is None:
        return  # nothing serving to extend yet
    terminal = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED}
    for case in cases:
        if case.case_id not in deferred:
            continue
        existing = (
            EnrollmentVerification.objects
            .filter(client=client, case=case)
            .exclude(stage__in=[s.value for s in terminal])
            .first()
        )
        if existing is not None:
            # NEVER park an actively-serving (or kitchen-assigned) enrollment. If
            # one is bound to a deferred FUTURE case it's a MIS-BINDING (the future
            # case's FK landed on the serving row), and parking it would STOP the
            # member's current service. Leave it running for the repoint remediation
            # to move it back onto the active case; only pre-serving funnel rows are
            # parked here.
            if EnrollmentStage(existing.stage) in (
                EnrollmentStage.SERVICE_ACTIVE,
                EnrollmentStage.ON_HOLD,
                EnrollmentStage.SERVICE_COMPLETE,
                EnrollmentStage.KITCHEN_ASSIGNMENT,
            ):
                continue
            if EnrollmentStage(existing.stage) != EnrollmentStage.SCHEDULED_EXTENSION:
                # Park a pre-existing (e.g. pending) enrollment for the reauth,
                # carrying the household's verified capture + roster first.
                _carry_verification_fields(existing, live)
                _create_missing_carried_profiles(existing, live)
                try:
                    advance_enrollment(
                        existing, EnrollmentStage.SCHEDULED_EXTENSION,
                        actor=actor, actor_label=actor_label,
                        note="Reauthorization parked; awaiting its authorization window.",
                        force=True, trigger="reauth_parked",
                    )
                except InvalidTransition:
                    existing.stage = EnrollmentStage.SCHEDULED_EXTENSION.value
                    existing.save(update_fields=["stage"])
            # Mirror kitchen/cadence as a WAITING schedule (idempotent; also
            # backfills an already-parked enrollment once the live kitchen/cadence
            # becomes known).
            _carry_waiting_schedule(existing, live, case)
            continue
        # Create a fresh parked enrollment carrying the household's verified data.
        new = EnrollmentVerification.objects.create(
            client=live.client,
            household=live.household,
            case=case,
            program_name=case.program_name or "",
            service_type=case.service_type or "",
            delivery_address=live.delivery_address,
            household_size=live.household_size,
            is_family_verified=live.is_family_verified,
            medicaid_type_verified=live.medicaid_type_verified,
            delivery_address_verified=live.delivery_address_verified,
            verified_at=live.verified_at,
            verified_by=live.verified_by,
            requested_by=live.requested_by,
            requested_at=live.requested_at,
            nutritionist_approved_at=live.nutritionist_approved_at,
            nutritionist_approved_by=live.nutritionist_approved_by,
            nutritionist_signature=live.nutritionist_signature,
            nutritionist_signature_image=live.nutritionist_signature_image,
            nutritionist_approval_pdf_key=live.nutritionist_approval_pdf_key,
            stage=EnrollmentStage.SCHEDULED_EXTENSION.value,
        )
        _create_missing_carried_profiles(new, live)
        _carry_waiting_schedule(new, live, case)
        # Record the "scheduled" event on the new enrollment's history (the
        # pre-existing-park path already logs one via advance_enrollment).
        _record_reauth_scheduled_event(new, actor=actor, actor_label=actor_label)


def _gap_pause_current_for_reauth(cur, *, actor=None, actor_label=""):
    """The current authorization window ended before the reauthorization's
    window begins (a GAP): complete the current enrollment and PAUSE its members
    so the household is off deliveries until the reauth activates. The pause is
    marked ``reauth_gap`` so the UI can surface a "Reauthorization" label."""
    from api.models import EnrollmentStage, MemberStatus
    from api.services.orders import truncate_future_deliveries

    if cur is None:
        return
    stage = EnrollmentStage(cur.stage)
    if stage not in (
        EnrollmentStage.SERVICE_COMPLETE,
        EnrollmentStage.CLOSED,
        EnrollmentStage.CANCELLED,
    ):
        try:
            advance_enrollment(
                cur, EnrollmentStage.SERVICE_COMPLETE, actor=actor,
                actor_label=actor_label,
                note="Authorization window ended; awaiting reauthorization window "
                     "(service paused).",
                force=True, trigger="reauth_gap",
            )
        except InvalidTransition:
            pass
    # Pause any still-active members for the duration of the gap.
    cur.member_profiles.filter(status=MemberStatus.ACTIVE).update(
        status=MemberStatus.PAUSED
    )
    # Attribute the pause to the reauthorization gap (UI label in Phase 5).
    if cur.close_reason != "reauth_gap":
        cur.close_reason = "reauth_gap"
        try:
            cur.save(update_fields=["close_reason"])
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        truncate_future_deliveries(cur)
    except Exception:  # pragma: no cover - defensive
        pass


def _refresh_scheduled_extension_from_live(waiting, live):
    """Refresh a parked extension from the CURRENT live serving enrollment right
    before activation, so it activates with the household's LATEST verification,
    delivery address, dietary/clinical data and roster -- not the (possibly
    months-old) park-time snapshot.

    Overwrites the enrollment's verified capture + address, overwrites each
    surviving member's DIETARY/clinical fields, and drops members no longer on the
    live roster. Service STATUS (and pause flags) are deliberately NOT copied: the
    live enrollment may be in a gap pause (members paused), which must not carry
    onto the activating extension -- the parked snapshot's status stands and the
    activation reconcile resumes service. New members added during the wait are
    created by ``_create_missing_carried_profiles`` in the close helper.
    """
    if waiting is None or live is None:
        return
    # Drop the display-only WAITING schedule so the activation carry
    # (_carry_service_and_activate -> create_member_delivery_schedules, which is
    # idempotent) rebuilds a fresh SCHEDULED plan + calendar from the live kitchen
    # / cadence.
    try:
        waiting.delivery_schedules.all().delete()
    except Exception:  # pragma: no cover - defensive
        pass
    for f in (
        "delivery_address", "household_size", "is_family_verified",
        "medicaid_type_verified", "delivery_address_verified",
        "verified_at", "verified_by", "requested_by", "requested_at",
        "nutritionist_approved_at", "nutritionist_approved_by",
        "nutritionist_signature", "nutritionist_signature_image",
        "nutritionist_approval_pdf_key",
    ):
        setattr(waiting, f, getattr(live, f))
    try:
        waiting.save()
    except Exception:  # pragma: no cover - defensive
        pass

    dietary_fields = [
        f for f in _CARRY_PROFILE_FIELDS
        if f not in ("status", "pause_locked", "eligibility_paused")
    ]
    live_by_client = {
        p.client_id: p for p in live.member_profiles.all() if p.client_id
    }
    for wp in list(waiting.member_profiles.all()):
        if not wp.client_id:
            continue
        lp = live_by_client.get(wp.client_id)
        if lp is None:
            # No longer on the live roster -> drop from the activating extension.
            wp.delete()
            continue
        for f in dietary_fields:
            setattr(wp, f, getattr(lp, f))
        try:
            wp.save(update_fields=dietary_fields)
        except Exception:  # pragma: no cover - defensive
            pass


def _activate_scheduled_extension(waiting, cur, reauth, *, actor=None, actor_label=""):
    """Promote a parked SCHEDULED_EXTENSION enrollment to Service Active, closing
    the current (being-extended) enrollment and linking it via ``supersedes``.
    Refreshes the parked enrollment from the current live enrollment first (latest
    verification / address / dietary / roster), then carries kitchen/cadence/
    calendar via the shared reuse-path helper; members resume active. Emits a
    timeline event."""
    from api.models import EnrollmentStage, TimelineEventType
    from api.services.timeline import emit_timeline_event

    terminal = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED}
    if cur is not None and EnrollmentStage(cur.stage) not in terminal:
        # Pull the freshest household data onto the parked enrollment BEFORE the
        # close (the close helper's carries are blank-fill only).
        _refresh_scheduled_extension_from_live(waiting, cur)
        # Closes cur, links supersedes, carries verification/roster, and drives
        # the waiting enrollment up to Service Active with the carried calendar.
        _close_old_and_link_to_existing(
            cur, waiting, reauth, actor, actor_label,
            note="Reauthorization window reached; service extended onto the new case.",
        )
        # Final roster alignment against the household membership (adds/reconciles
        # any member tied only via the household roster).
        try:
            from api.serializers import sync_household_members
            sync_household_members(waiting.client, enrollment=waiting)
        except Exception:  # pragma: no cover - defensive
            pass
    else:
        # No current serving enrollment to close -> just activate the waiting one.
        _step_stage_forward(
            waiting, EnrollmentStage.SERVICE_ACTIVE, actor=actor,
            actor_label=actor_label, note="Reauthorization activated.",
            trigger="reauth_activated",
        )

    emit_timeline_event(
        client=waiting.client,
        event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED,
        occurred_at=timezone.now(),
        title="Service extended via reauthorization",
        subtitle=(
            "The reauthorization's authorization window began; service continues "
            "on the new case."
        ),
        enrollment=waiting,
        case=reauth,
        source="system",
        actor=actor_label or "",
    )


def process_scheduled_extensions(
    *, client=None, today=None, apply=True, actor=None, actor_label=""
):
    """Advance parked reauthorization extensions by the calendar.

    For each ``SCHEDULED_EXTENSION`` enrollment (optionally scoped to ``client``),
    with the current window ending at ``E1`` and the reauth window starting at
    ``S2``:

      * ``today >= max(E1, S2)`` -> ACTIVATE (promote to Service Active, close the
        current enrollment).
      * ``E1 < today < S2``      -> GAP (current -> Service Complete, members
        paused until the reauth window begins).
      * otherwise                -> still WAITING (current keeps serving).

    Returns counts ``{activated, gapped, waiting, skipped, discarded}``.
    ``apply=False`` is a dry run (counts only)."""
    from api.models import EnrollmentStage, EnrollmentVerification

    today = today or timezone.localdate()
    qs = (
        EnrollmentVerification.objects
        .filter(stage=EnrollmentStage.SCHEDULED_EXTENSION)
        .select_related("client", "case", "household")
    )
    if client is not None:
        qs = qs.filter(client=client)

    result = {"activated": 0, "gapped": 0, "waiting": 0, "skipped": 0, "discarded": 0}
    for waiting in qs:
        reauth = waiting.case
        if reauth is None:
            result["skipped"] += 1
            continue
        # The reauth case closed/cancelled before it could activate -> discard the
        # parked enrollment (it will never take over).
        if reauth.case_status in _CLOSED_CASE_STATUSES:
            if apply:
                try:
                    advance_enrollment(
                        waiting, EnrollmentStage.CLOSED, actor=actor,
                        actor_label=actor_label,
                        note="Reauthorization case closed before activation; parked "
                             "extension discarded.",
                        force=True, trigger="reauth_discarded",
                    )
                except InvalidTransition:
                    _force_close_enrollment(waiting)
                set_reauth_attention(waiting.client, False)
                sync_reauthorized_tag(waiting.client)
            result["discarded"] += 1
            continue
        s2, _e2 = reauth.effective_authorization_window()
        if s2 is None:
            result["skipped"] += 1
            continue
        cur = _primary_enrollment(waiting.client)  # excludes the waiting row
        e1 = None
        if cur is not None and cur.case is not None:
            _s1, e1 = cur.case.effective_authorization_window()
        boundaries = [d.date() for d in (e1, s2) if d is not None]
        switch_date = max(boundaries) if boundaries else s2.date()

        if today >= switch_date:
            if apply:
                _activate_scheduled_extension(
                    waiting, cur, reauth, actor=actor, actor_label=actor_label,
                )
                # Clean handoff -> clear any Reauth Attention flag, and refresh
                # the Reauthorized indicator (off once no parked reauth remains).
                set_reauth_attention(waiting.client, False)
                sync_reauthorized_tag(waiting.client)
            result["activated"] += 1
        elif e1 is not None and today > e1.date():
            if apply:
                _gap_pause_current_for_reauth(
                    cur, actor=actor, actor_label=actor_label,
                )
                # A service gap needs a human eye.
                set_reauth_attention(waiting.client, True)
            result["gapped"] += 1
        else:
            if apply:
                # Still parked -> ensure the "Reauthorized" indicator is present.
                sync_reauthorized_tag(waiting.client)
            result["waiting"] += 1
    return result


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
    "closed_out", "cancelled", "downgraded", "service_inactive",
    "reactivated"}``.
    """
    result = {
        "sole_denied": False, "paused": False,
        "closed_out": False, "cancelled": False, "downgraded": False,
        "service_inactive": False, "reactivated": False, "switched": False,
        "scope_switched": False, "disregarded": False, "ineligible": False,
        "replaced": False,
    }
    from api.models import EnrollmentVerification

    cases = _internal_service_cases(client)
    if not cases:
        recompute_client_stage(client, actor=actor)
        return result

    open_cases = open_internal_service_cases(client)
    governing = pick_governing_case(cases)
    # Heal the 'serving enrollment on the wrong case' split at the source FIRST: a
    # member serving without a governing case (its case stranded on a stray) OR
    # mis-bound to a DEFERRED future case is repointed onto the active governing
    # case. Done BEFORE parking so the future case is freed and then parked as its
    # OWN row (never the serving one), and before the auth branches read
    # enrollment.case.
    _bind_governing_case_to_serving_enrollment(client, governing)
    # Ensure any deferred future reauthorization has a parked (non-serving)
    # SCHEDULED_EXTENSION enrollment, so it's visible + ready to activate later
    # WITHOUT supplanting the serving case now.
    _park_deferred_extensions(
        client, cases, actor=actor, actor_label=actor_label,
    )
    # Close any parked reauth whose case has since closed/cancelled, so a closed
    # case never leaves a dangling SCHEDULED_EXTENSION row behind.
    _close_orphaned_scheduled_extensions(
        client, actor=actor, actor_label=actor_label,
    )
    # Positive "Reauthorized" indicator: on while a reauth is parked, off once
    # none remain.
    sync_reauthorized_tag(client)
    # Flag for a human ONLY when a reauth can't be auto-extended (kind/scope
    # mismatch). A cleanly-deferrable reauth needs no attention here (a GAP later
    # applies the tag from the daily task). Never clears a gap-applied tag: if
    # there's no mismatch we only clear when no waiting extension remains.
    if _reauth_kind_scope_mismatch(cases):
        set_reauth_attention(client, True)
    elif not EnrollmentVerification.objects.filter(
        client=client, stage=EnrollmentStage.SCHEDULED_EXTENSION,
    ).exists():
        set_reauth_attention(client, False)
    # Record an old -> new governing-case switch (timeline event + primary note)
    # before acting on it, so the history captures WHY service state changed.
    _record_governing_case_change(
        client, governing, actor=actor, actor_label=actor_label,
    )
    gov_status = governing.service_authorization_status
    # SERVICE_INACTIVE is sticky: remember it here so the tail can RE-DERIVE past
    # it (ignore_sticky) + emit the reactivation event once an open case reopens
    # service below.
    was_service_inactive = client.lifecycle_stage == ClientStage.SERVICE_INACTIVE
    # A CASE-DRIVEN INELIGIBLE off-ramp (Kitchen Assignment whose case closed or
    # was denied) is ALSO reversible: it must be lifted when a new open, favorable
    # case reopens service. Distinguished from a genuine (assessment) ineligibility
    # by its ineligible_reasons, so we never clear a real ineligibility here.
    _KA_OFFRAMP_REASONS = {_KA_CLOSED_INELIGIBLE_REASON, _KA_DENIED_INELIGIBLE_REASON}
    was_case_ineligible = (
        client.lifecycle_stage == ClientStage.INELIGIBLE
        and bool(client.ineligible_reasons)
        and set(client.ineligible_reasons or []) <= _KA_OFFRAMP_REASONS
    )

    if not open_cases:
        # CLOSURE full stop: the client's LAST open internal-service case has
        # closed. Reversibly stop service -- truncate future deliveries + pause
        # (On Hold) + note the primary, THEN park at the SERVICE_INACTIVE
        # off-ramp. NOT a cancel, so a later open case resumes it. Opens NO
        # tickets -- the timeline, StageEvents and primary note carry visibility.
        # Task 4.3: a household still only at Kitchen Assignment when its last
        # open case closes never became an active member -> hard off-ramp to
        # INELIGIBLE. Set BEFORE the close-out so it isn't also parked at the
        # reversible SERVICE_INACTIVE off-ramp (INELIGIBLE outranks it).
        if _has_kitchen_assignment_enrollment(client) and _mark_kitchen_assignment_ineligible(
            client, reason=_KA_CLOSED_INELIGIBLE_REASON,
            actor=actor, actor_label=actor_label,
        ):
            result["ineligible"] = True
        outcome = _full_stop_close_out(
            client, governing, actor=actor, actor_label=actor_label,
        )
        result["closed_out"] = True
        result["paused"] = outcome["paused"]
        result["service_inactive"] = outcome["service_inactive"]
    elif gov_status in _DENIED_EQUIVALENT_STATUSES:
        # Governing meal/box authorization is denied -- or NEVER_REQUESTED, which
        # is treated identically to a denial (an open case that confers no
        # service). No favorable/pending open case exists, whether one case or
        # several -> full stop: pause every servable enrollment (incl. Active --
        # Rule 3), disregard a still-pending-verification request, and off-ramp a
        # Kitchen-Assignment household to INELIGIBLE.
        result["sole_denied"] = True
        # Task 4.3: capture Kitchen-Assignment membership BEFORE the loop pauses
        # those enrollments (KA -> On Hold), so a denied authorization at Kitchen
        # Assignment can hard off-ramp the client to INELIGIBLE afterwards.
        had_kitchen_assignment = _has_kitchen_assignment_enrollment(client)
        for enr in _governing_enrollments(client):
            stage = EnrollmentStage(enr.stage)
            if stage in _DENIAL_PAUSE_STAGES:
                try:
                    advance_enrollment(
                        enr, EnrollmentStage.ON_HOLD, actor=actor,
                        actor_label=actor_label, note=_DENIAL_HOLD_NOTE,
                        trigger="reconcile.authorization_denied",
                    )
                    result["paused"] = True
                except InvalidTransition:
                    pass
            elif stage == EnrollmentStage.PENDING_VERIFICATION:
                # Objective 3 / task 4.1: a denial while still awaiting
                # verification removes the request (DISREGARDED) -- the member
                # leaves the Verification queue. Not auto-resumed on re-approval.
                try:
                    advance_enrollment(
                        enr, EnrollmentStage.DISREGARDED, actor=actor,
                        actor_label=actor_label, note=_DENIAL_DISREGARD_NOTE,
                        trigger="reconcile.authorization_denied",
                    )
                    result["disregarded"] = True
                except InvalidTransition:
                    pass
        if had_kitchen_assignment and _mark_kitchen_assignment_ineligible(
            client, reason=_KA_DENIED_INELIGIBLE_REASON,
            actor=actor, actor_label=actor_label,
        ):
            result["ineligible"] = True
    elif gov_status in (
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    ):
        # Favorable authorization -> resume anything this rule auto-paused AND
        # advance a verified household to Kitchen Assignment (Rule 2). Routing
        # the advance through here means it fires on EVERY case-save path
        # (extension, manual import, bulk CLI), not just the manual import.
        #
        # A governing-case replacement (new case id, possibly different product
        # kind or scope) takes precedence: it closes the old enrollment and opens
        # a new one, so skip the normal resume/advance below.
        if replace_enrollment_for_case_change(
            client, governing,
            actor=actor, actor_label=actor_label,
        ):
            result["switched"] = True
            result["replaced"] = True
        else:
            # No live enrollment but an open, approved case exists: the prior
            # enrollment closed (its case ended) and a NEW case has since arrived.
            # Reopen from the prior's data (re-verify only if the no-open-case gap
            # exceeded 60 days) so the household isn't stranded at Not Eligible
            # with a valid approved case. Self-guards (no-op when a live
            # enrollment already exists or the case is held by another live/
            # cross-client row), so it's safe to call unconditionally. Never let a
            # reopen edge case crash the whole reconcile.
            try:
                if reopen_enrollment_for_new_case(
                    client, governing, actor=actor, actor_label=actor_label,
                ) is not None:
                    result["reopened"] = True
            except Exception:  # pragma: no cover - defensive
                pass
            for enr in _governing_enrollments(client):
                if EnrollmentStage(enr.stage) == EnrollmentStage.ON_HOLD:
                    # Resume whichever auto-pause holds this enrollment: a denial
                    # hold, or -- on reactivation -- the closure hold. Each is a
                    # no-op unless the last hold's note matches, so calling both
                    # is safe and order-independent.
                    _resume_auto_paused_enrollment(enr, actor=actor)
                    _resume_auto_paused_enrollment(
                        enr, actor=actor, hold_note=_CLOSURE_HOLD_NOTE,
                        resume_note=(
                            "Auto-resumed: new open internal-service case "
                            "reopened service."
                        ),
                    )
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
    # already truncated and the enrollments placed On Hold). Best-effort.
    if not result["closed_out"]:
        for enr in _governing_enrollments(client):
            try:
                reconcile_delivery_state(enr, actor=actor)
            except Exception:  # pragma: no cover - defensive
                pass

    # Reactivation: a member parked at a REVERSIBLE off-ramp now has an open case
    # again -> re-derive past the sticky off-ramp and announce it once.
    #   - SERVICE_INACTIVE: any open case reopens service.
    #   - case-driven INELIGIBLE (KA case closed/denied): only a FAVORABLE open
    #     case (approved / not-required) reopens it -- a pending/denied reopen
    #     leaves the household off-ramped. Its stale ineligible_reasons are
    #     cleared so the flag doesn't linger after the stage lifts.
    gov_favorable = gov_status in (
        ServiceAuthorizationStatus.APPROVED,
        ServiceAuthorizationStatus.NOT_REQUIRED,
    )
    lift_case_ineligible = was_case_ineligible and gov_favorable and bool(open_cases)
    if lift_case_ineligible and client.ineligible_reasons:
        client.ineligible_reasons = []
        client.save(update_fields=["ineligible_reasons"])
    reactivated = (was_service_inactive and bool(open_cases)) or lift_case_ineligible
    # Always lift the sticky SERVICE_INACTIVE / case-INELIGIBLE off-ramp so the
    # member re-derives from live data (a new open case may move them back to
    # navigation / pending / service).
    recompute_client_stage(client, actor=actor, ignore_sticky=reactivated)
    # ...but only ANNOUNCE a "Service Reactivated" when they actually had service
    # to resume -- i.e. their verification was completed. A NEVER-VERIFIED member
    # (verified_at never set: no kitchen/cadence, never served) merely returns to
    # navigation/pending, so emitting "Service Reactivated" for them is wrong.
    if (
        reactivated
        and client.lifecycle_stage != ClientStage.SERVICE_INACTIVE
        and verification_completed(client)
    ):
        from api.services import timeline

        author = actor_label or _actor_name(actor)
        _write_primary_system_note(
            client,
            (
                f"Service reactivated on {timezone.localdate().isoformat()}: a new "
                f"open internal-service case reopened service."
            ),
            author_name=author,
        )
        timeline.event_for_member_service_reactivated(client, actor=author)
        result["reactivated"] = True

    # Keep the denormalized Members-list sort key fresh (case add/close/date
    # change may have moved the latest internal-service case date).
    try:
        refresh_internal_case_sort(client)
    except Exception:  # pragma: no cover - defensive
        pass

    # Refresh the member/household warning snapshot after a case-driven change
    # (fires on both extension case saves and CSV imports, which route through
    # CaseSerializer -> here). Best-effort; lazy import avoids a circular dep.
    try:
        from api.services.warnings import sync_client_warnings

        sync_client_warnings(client)
    except Exception:  # pragma: no cover - defensive
        pass

    return result

"""Member-scoped portal endpoints: list, detail, and the profile sub-tabs
(insurance, social coverage, history, orders, household, notes, tickets) plus
the verification wizard write."""

import logging
import re
import uuid
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.db.models import (
    Case as SQLCase,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    Address,
    Cadence,
    Case,
    CaseHouseholdType,
    CaseStatus,
    CaseType,
    Client,
    ClientPhone,
    ClientPhoneSource,
    ClientStage,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    HouseholdMember,
    Insurance,
    InsurancePlanType,
    Kitchen,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    KitchenProductType,
    MemberStatus,
    MenuType,
    Note,
    MemberWarning,
    NoteSource,
    ProductType,
    ProductTypeKind,
    PurchaseOrder,
    ServiceAuthorizationStatus,
    StageEvent,
    Ticket,
    TicketSeverity,
    TicketSource,
    TicketStatus,
    TicketType,
    TicketTypeCode,
    TimelineBadgeTone,
    TimelineEvent,
    TimelineEventType,
    UniteUsAgent,
    WarningStatus,
)
from ..views_phones import _phone_dict
from ..services.catalog import (
    assign_product_type_for_internal_service,
    detected_product_kind_for_enrollment,
    menu_type_for_member,
    product_kind_for_enrollment,
    product_type_kind_for_name,
)
from ..services.delivery import (
    active_cadence_codes,
    cadence_needs_weekday,
    cadence_options_for_kind,
    create_member_delivery_schedules,
    current_household_cadence,
    update_household_cadence,
)
from ..services.client_diagnostic import diagnose_client
from ..services.orders import (
    _format_address,
    generate_delivery_calendar,
    rebuild_delivery_calendar,
    resync_scheduled_orders,
    sync_delivery_calendar,
)
from ..services.kitchens import (
    kitchen_offered_menu_index,
    kitchen_options,
    required_product_for_program,
    serving_kitchens_for_member,
)
from ..services.meal_rules import reconcile_member_kitchen_output, resolve_kitchen_meal
from ..services.lifecycle import (
    InvalidTransition,
    advance_enrollment,
    clear_new_flag_on_verification_request,
    governing_internal_case,
    governing_pending_enrollment,
    has_open_internal_service_case,
    has_valid_medicaid,
    has_valid_social_care,
    is_urgent_care_candidate,
    recompute_client_stage,
    recompute_enrollment_household,
    reconcile_enrollment_authorization,
)
from ..services import timeline
from ..services.warnings import sync_household_warnings
from ..serializers import (
    add_client_to_household,
    ensure_household_with_primary,
    ensure_primary_of_own_household,
    search_clients,
    sync_household_members,
)
from .base import PortalAPIView, PortalGenericAPIView, current_agent
from .permissions import IsManagementAgent, is_management_group
from . import serializers as s

logger = logging.getLogger(__name__)

# System note left on a member when their menu type / dietary needs can't be
# fulfilled by any (or the assigned) kitchen and they're pulled Out of Orbit.
NO_KITCHEN_OUT_OF_ORBIT_NOTE = (
    "No kitchen currently supports this member's dietary needs."
)


def _agent_actor(agent):
    """Timeline ``actor`` attribution for an acting agent. Prefer the agent code
    (``agent:<code>``, resolved to a name via a batched lookup), but many
    portal agents (e.g. Management/CS) have no code -- fall back to the agent's
    name (``user:<name>``) so the history still shows WHO performed the action
    instead of a blank attribution."""
    if agent is None:
        return ""
    if agent.agent_code:
        return f"agent:{agent.agent_code}"
    if agent.name:
        return f"user:{agent.name}"
    return ""


# Marker prefix on the auto-generated Out-of-Range Case Closure ticket's reason,
# used to find (and later resolve) the ticket THIS process opened without
# clobbering an unrelated Case Closure ticket an agent raised by hand.
_OUT_OF_RANGE_TICKET_MARKER = "Out-of-range ZIP code:"


def _case_closure_ticket_type():
    """The Case Closure TicketType (seeded on the fly if not present)."""
    obj, _ = TicketType.objects.get_or_create(
        code=TicketTypeCode.CASE_CLOSURE,
        defaults={"label": TicketTypeCode.CASE_CLOSURE.label},
    )
    return obj


def _open_out_of_range_ticket(enrollment, reason):
    """Open a Case Closure ticket on the household primary for an out-of-range
    ZIP unless an unresolved out-of-range one already exists. Returns True if a
    ticket was created."""
    client = getattr(enrollment, "client", None)
    if client is None:
        return False
    type_obj = _case_closure_ticket_type()
    exists = (
        Ticket.objects.filter(
            client=client, type=type_obj,
            reason__startswith=_OUT_OF_RANGE_TICKET_MARKER,
        )
        .exclude(status=TicketStatus.RESOLVED)
        .exists()
    )
    if exists:
        return False
    Ticket.objects.create(
        type=type_obj,
        status=TicketStatus.OPEN,
        severity=TicketSeverity.HIGH,
        source=TicketSource.OTHER,
        reason=reason,
        client=client,
        case=getattr(enrollment, "case", None),
    )
    return True


def _resolve_out_of_range_tickets(enrollment, actor=""):
    """Mark this household's open Out-of-Range Case Closure ticket(s) resolved
    (called when the ZIP becomes serviceable again). Returns the count resolved."""
    client = getattr(enrollment, "client", None)
    if client is None:
        return 0
    qs = Ticket.objects.filter(
        client=client, type__code=TicketTypeCode.CASE_CLOSURE,
        reason__startswith=_OUT_OF_RANGE_TICKET_MARKER,
    ).exclude(status=TicketStatus.RESOLVED)
    return qs.update(
        status=TicketStatus.RESOLVED,
        resolved_at=timezone.now(),
        resolved_by=actor,
    )


def _hold_household_for_range(enrollment, author):
    """Place the whole household On Hold for an out-of-range ZIP (no-op when it
    is already On Hold or the transition isn't allowed)."""
    if EnrollmentStage(enrollment.stage) == EnrollmentStage.ON_HOLD:
        return False
    try:
        advance_enrollment(
            enrollment, EnrollmentStage.ON_HOLD,
            note=(
                f"Automatically placed on hold — delivery ZIP outside coverage "
                f"area (Out of Range).{f' Actioned via {author}.' if author else ''}"
            ),
        )
        return True
    except InvalidTransition:
        return False


def _enrollment_resume_blocked_by_ineligibility(enrollment):
    """True when a held program must NOT be manually resumable because there is
    NO servable member left -- everyone is on the hard INELIGIBLE off-ramp
    (out-of-range address, wrong Medicaid type, expired/missing insurance).

    An INELIGIBLE member is now paused INDIVIDUALLY (see
    ``api.services.eligibility``) and excluded from the schedule, so a single
    ineligible member no longer blocks resuming the rest of the household. Resume
    is blocked only when there is no eligible member to serve: no member profile
    is eligible AND the case-holder themselves is ineligible. An INELIGIBLE hold
    is CareCircle-unfixable -- only recovering the underlying data (a later import
    re-running ``reconcile_client_eligibility``) lifts it. Idempotent read-only.
    """
    if enrollment is None:
        return False
    # At least one member whose client is NOT ineligible -> there is someone to
    # serve, so the program may be resumed (ineligible members stay paused).
    eligible_member_exists = enrollment.member_profiles.exclude(
        client__lifecycle_stage=ClientStage.INELIGIBLE
    ).exists()
    if eligible_member_exists:
        return False
    holder = getattr(enrollment, "client", None)
    return holder is not None and holder.lifecycle_stage == ClientStage.INELIGIBLE


# Message shown when a program-tab write action is rejected because the
# program's governing internal-service case is closed (no open case remains).
_PROGRAM_LOCKED_MESSAGE = (
    "This program is closed — its internal-service case is no longer open, so "
    "no further changes can be made. Service resumes automatically only when a "
    "new open internal-service case is created for the member."
)


def _program_locked(enrollment):
    """True when the enrollment's program is CLOSED — the household HAS an
    internal-service (meal/box) case but NONE is still open, so its verification
    enrollment is frozen (read-only history). Every program-tab write action is
    refused in this state.

    Requires at least one internal-service case: a household with no such case
    yet (e.g. a pre-case enrollment) is NOT "closed" — it simply has nothing to
    govern it — so it stays editable. Once a case existed and all of them closed,
    the program is off service and only a NEW open case reopens it (via the
    reversible reconcile path). Keyed on the enrollment owner's cases (the primary
    owns the household's case), matching ``governing_internal_case`` / the PO
    guardrail. Read-only.
    """
    if enrollment is None:
        return False
    holder = getattr(enrollment, "client", None)
    if holder is None:
        return False
    has_internal_case = any(
        c.case_type == CaseType.INTERNAL_SERVICE for c in holder.cases.all()
    )
    return has_internal_case and not has_open_internal_service_case(holder)


def _program_locked_response():
    """The standard 400 returned when a program-tab action is blocked because the
    governing case is closed. Centralized so every guarded endpoint speaks with
    one voice."""
    return Response(
        {"error": _PROGRAM_LOCKED_MESSAGE},
        status=http.HTTP_400_BAD_REQUEST,
    )


def _prior_enrollment_chain(enrollment, limit=25):
    """Walk the supersession chain of ``enrollment`` (enr.supersedes, then its
    supersedes, ...) and return a newest-first list of the older, replaced
    enrollments. Each entry carries the display fields the Program tab needs to
    render a read-only accordion (with its own history sub-tab)."""
    chain = []
    seen = set()
    cur = enrollment.supersedes if enrollment else None
    while cur is not None and cur.pk not in seen and len(chain) < limit:
        seen.add(cur.pk)
        case = cur.case
        chain.append({
            "id": cur.pk,
            "code": cur.code,
            "stage": cur.stage,
            "case_id": str(case.case_id) if case else None,
            "program_name": cur.program_name or "",
            "service_type": cur.service_type or "",
            "kitchen_name": cur.kitchen.name if cur.kitchen_id else "",
            "close_reason": cur.close_reason or "",
            "verified_at": cur.verified_at.isoformat() if cur.verified_at else None,
        })
        cur = cur.supersedes
    return chain


def _resume_household_after_range(enrollment):
    """Resume a household that was auto-held for an out-of-range ZIP, back to the
    stage it was held from (defaulting to Service Active). No-op when it isn't
    currently On Hold."""
    if EnrollmentStage(enrollment.stage) != EnrollmentStage.ON_HOLD:
        return False
    last_hold = StageEvent.objects.filter(
        enrollment=enrollment, to_stage=EnrollmentStage.ON_HOLD,
    ).first()
    target = EnrollmentStage.SERVICE_ACTIVE
    if last_hold and last_hold.from_stage:
        try:
            target = EnrollmentStage(last_hold.from_stage)
        except ValueError:
            target = EnrollmentStage.SERVICE_ACTIVE
    try:
        advance_enrollment(
            enrollment, target, force=True,
            note="Service resumed — delivery ZIP now within coverage area.",
        )
        return True
    except InvalidTransition:
        return False


_ALL_PAUSED_HOLD_NOTE = "Automatically placed on hold — all household members paused."
_ALL_PAUSED_RESUME_NOTE = "Service resumed — a household member returned from pause."


def _reconcile_all_paused_hold(enrollment):
    """Roll a manual member pause up to the PROGRAM.

    A manual member pause doesn't stop the program on its own, but once EVERY
    member of the household is paused there is no one left to serve -- so the
    enrollment is placed On Hold. Unpausing any member resumes it to the stage it
    was held from. Note-scoped: it only reverses ITS OWN auto-hold, so a manual
    Place-on-Hold (or a hold from a different rule) is never silently overridden.
    No-op when the household has no member profiles. Best-effort + idempotent.
    """
    profiles = list(enrollment.member_profiles.all())
    if not profiles:
        return
    all_paused = all(p.status == MemberStatus.PAUSED for p in profiles)
    stage = EnrollmentStage(enrollment.stage)

    if all_paused and stage != EnrollmentStage.ON_HOLD:
        try:
            advance_enrollment(
                enrollment, EnrollmentStage.ON_HOLD, note=_ALL_PAUSED_HOLD_NOTE,
            )
        except InvalidTransition:
            pass
        return

    if not all_paused and stage == EnrollmentStage.ON_HOLD:
        last_hold = StageEvent.objects.filter(
            enrollment=enrollment, to_stage=EnrollmentStage.ON_HOLD,
        ).first()  # StageEvent orders by -entered_at, so this is the latest hold
        if not last_hold or not (last_hold.note or "").startswith(_ALL_PAUSED_HOLD_NOTE):
            return  # held by a different rule / a manual hold -- leave it alone
        target = EnrollmentStage.SERVICE_ACTIVE
        if last_hold.from_stage:
            try:
                target = EnrollmentStage(last_hold.from_stage)
            except ValueError:
                target = EnrollmentStage.SERVICE_ACTIVE
        try:
            advance_enrollment(
                enrollment, target, force=True, note=_ALL_PAUSED_RESUME_NOTE,
            )
        except InvalidTransition:
            pass


def _enforce_delivery_coverage(enrollment, agent, *, allow_reactivate=False):
    """Delivery Coverage Eligibility Check for every member of ``enrollment``.

    A member whose enrollment's DELIVERY address OR their PRIMARY (Current/Home)
    address ZIP is in the excluded-ZIP list is set Out of Range (reason "Delivery
    Address Outside Coverage Area"), with a system note + timeline event
    attributed to ``agent``. Manually Paused / Inactive members are left
    untouched. When any member is newly set Out of Range the whole household is
    placed On Hold and a Case Closure ticket is opened (pre-filled with the
    offending ZIP) for an agent to review.

    When ``allow_reactivate`` is True (the delivery ZIP just became serviceable)
    an Out-of-Range member is re-checked against the kitchen-aware meal rule
    (which itself re-checks both addresses) and returned to Active only if it now
    passes (so a dietary/kitchen block or a still-excluded primary ZIP keeps them
    excluded). If that clears every member, the auto-hold is resumed and the
    Out-of-Range Case Closure ticket is resolved.

    Returns ``{"out_of_range": [...], "reactivated": [...]}``.
    """
    from ..services.service_area import (
        SERVICE_AREA_REASON,
        excluded_zips,
        member_excluded_info,
        out_of_range_ticket_reason,
        service_area_note_body,
    )

    excluded = excluded_zips()
    actor = _agent_actor(agent)
    author = agent.name if agent else ""
    out_names = []
    reactivated_names = []
    # The first offending ZIP/source seen, used to pre-fill the closure ticket.
    ticket_zip, ticket_source = "", "delivery address"

    def _member_name(m):
        c = getattr(m, "client", None)
        name = f"{getattr(c, 'first_name', '')} {getattr(c, 'last_name', '')}".strip()
        return name or (str(c.pk) if c else "Member")

    for mv in enrollment.member_profiles.select_related("client").all():
        offending_zip, source = member_excluded_info(mv, excluded=excluded)
        if offending_zip:
            # An out-of-range ZIP is a household-wide geographic block: the whole
            # household shares the delivery address, so EVERY non-terminal member
            # is set Out of Range (individually excluded from future POs /
            # deliveries and individually countable), not just the Active ones.
            # We skip only INACTIVE (terminal off-boarded) and members already
            # Out of Range (idempotent -- avoids duplicate note/timeline rows).
            if mv.status in (MemberStatus.INACTIVE, MemberStatus.OUT_OF_RANGE):
                continue
            mv.status = MemberStatus.OUT_OF_RANGE
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            mv.save(update_fields=[
                "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
            ])
            out_names.append(_member_name(mv))
            if not ticket_zip:
                ticket_zip, ticket_source = offending_zip, source
            try:
                timeline.event_for_out_of_range(
                    mv, enrollment=enrollment, reason=SERVICE_AREA_REASON,
                    zip_code=offending_zip, actor=actor,
                )
            except Exception:  # never let history-logging break the save
                pass
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.SYSTEM,
                        author_name=author,
                        body=service_area_note_body(offending_zip, source),
                    )
                except Exception:  # never let note-writing break the save
                    pass
        elif allow_reactivate and mv.status == MemberStatus.OUT_OF_RANGE:
            # ZIP is now serviceable: return to Active only if the meal rule
            # (now ZIP-aware) also passes. A dietary/kitchen block leaves them
            # excluded (reconcile mutates mv in memory; we only persist when it
            # clears). Explicit restore-range flow, so allow_resume=True lets the
            # meal rule move the member OFF OUT_OF_RANGE (the ZIP re-check still
            # keeps them excluded if the ZIP is still out of coverage).
            out, _became, _reason = reconcile_member_kitchen_output(
                mv, enrollment.kitchen, save=False, allow_resume=True,
            )
            if not out:
                mv.save()
                reactivated_names.append(_member_name(mv))
                try:
                    timeline.event_for_member_reactivated(
                        mv, enrollment=enrollment, actor=actor,
                    )
                except Exception:  # never let history-logging break the save
                    pass

    # Side effects: opening members drive a household hold + Case Closure ticket;
    # a full reactivation (no member still Out of Range) resumes the hold and
    # resolves the ticket.
    if out_names:
        try:
            _open_out_of_range_ticket(
                enrollment,
                out_of_range_ticket_reason(ticket_zip, ticket_source, out_names),
            )
        except Exception:  # never let ticket-writing break the coverage check
            pass
        try:
            _hold_household_for_range(enrollment, author)
        except Exception:  # never let the hold break the coverage check
            pass
    elif reactivated_names:
        still_out = enrollment.member_profiles.filter(
            status=MemberStatus.OUT_OF_RANGE,
        ).exists()
        if not still_out:
            try:
                _resume_household_after_range(enrollment)
            except Exception:
                pass
            try:
                _resolve_out_of_range_tickets(enrollment, actor=actor)
            except Exception:
                pass

    return {"out_of_range": out_names, "reactivated": reactivated_names}


# Reverse of serializers._STATUS_MAP: a filter value -> the lifecycle stages it covers.
# Verification is a yes/no fact (Pending Verification / Verified), so those two
# chips are NOT in this map -- they are resolved via verification_completed_q()
# (the verified_at fact), not lifecycle_stage. Authorization is a separate
# dimension handled by the `authorization` filter param.
STATUS_TO_STAGES = {
    "Denied": ["not_eligible"],
    "Kitchen Assignment": ["kitchen_assignment"],
    "Active": ["active"],
    "Completed": ["completed"],
}

# Authorization filter value -> (matching statuses, statuses that OUTRANK it).
# A client is shown under a given authorization only when their GOVERNING
# internal-service case has it -- i.e. they hold a case with a matching status
# and none with a more favorable one. This mirrors lifecycle.governing_case_key
# favorability (approved/not_required > pending > denied), so the filter agrees
# with the Authorization badge (which reflects the governing case). Without the
# outrank exclusion, a client with both a denied and a pending case would wrongly
# appear under "Denied" while their badge reads "Waiting Authorization".
AUTHORIZATION_FILTERS = {
    "approved": (["approved", "not_required"], []),
    "pending": (["pending"], ["approved", "not_required"]),
    "denied": (["denied"], ["approved", "not_required", "pending"]),
}


def apply_authorization_filter(qs, value):
    """Restrict ``qs`` to clients whose GOVERNING internal-service case has the
    given authorization ``value``. Caller handles ``.distinct()``."""
    spec = AUTHORIZATION_FILTERS.get(value)
    if not spec:
        return qs
    match_statuses, outrank = spec
    # Correlated per-client subquery over the SAME internal-service case row.
    # NB: a plain ``.exclude(cases__case_type=INTERNAL, cases__status__in=...)``
    # does NOT tie the two conditions to the same case (Django's multi-valued
    # exclude splits them), so it wrongly drops a member whose meal case is
    # pending but whose *eligibility* case happens to be approved/not_required.
    # Anchoring both on one Case via Exists keeps the match/outrank semantics
    # scoped to the internal-service (meal/box) case that actually governs.
    internal_cases = Case.objects.filter(
        client=OuterRef("pk"),
        case_type=CaseType.INTERNAL_SERVICE,
    )
    qs = qs.filter(
        Exists(internal_cases.filter(service_authorization_status__in=match_statuses))
    )
    if outrank:
        # Drop clients holding a more favorable internal-service authorization
        # (that more favorable case would be the governing one instead).
        qs = qs.exclude(
            Exists(internal_cases.filter(service_authorization_status__in=outrank))
        )
    return qs


# Enrollment stages that are NOT the member's current/live service: a dietary
# profile attached to one of these is HISTORY (a superseded/closed/off-boarded
# enrollment) and must not drive a "current member status" filter.
_NON_CURRENT_ENROLLMENT_STAGES = [
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.DISREGARDED,
    EnrollmentStage.SERVICE_COMPLETE,
]



def current_member_status_exists(member_status, *, eligibility_paused=None):
    """``Exists`` over the client's member dietary profiles in ``member_status``
    on a CURRENT (non-terminal) enrollment.

    The individual member-status flags (Out of Orbit / Out of Range / Paused) are
    a CURRENT state, but a client accumulates a profile per enrollment -- so a
    plain ``member_profiles__status=X`` also matches a STALE status on a closed or
    superseded enrollment (e.g. an out-of-orbit profile left behind by a
    governing-case replacement), surfacing members whose live profile is Active.
    Scoping to a non-terminal enrollment fixes that, and ``Exists`` avoids the
    join duplicates a multi-valued ``.filter`` would add.

    ``eligibility_paused`` (True/False) further splits Paused into the auto
    (eligibility-driven) vs manual (agent) pause; None leaves it unfiltered."""
    profiles = (
        MemberDietaryProfile.objects
        .filter(client=OuterRef("pk"), status=member_status)
        .exclude(enrollment__stage__in=[s.value for s in _NON_CURRENT_ENROLLMENT_STAGES])
    )
    if eligibility_paused is not None:
        profiles = profiles.filter(eligibility_paused=eligibility_paused)
    return Exists(profiles)


# Page-level base scope: restricts the list to the lifecycle stages a given
# work area cares about (independent of the per-status filter chips).
SCOPE_TO_STAGES = {
    # Verification work area: households whose verification was requested
    # (pending_verification) or completed (verified). An approved household
    # advances to kitchen_assignment and moves to the logistics work area.
    "verification": ["pending_verification", "verified"],
    "logistics": ["kitchen_assignment"],
}

# select_related the requested_by / verified_by agents on the enrollment prefetch
# so the Verification list's agent columns don't trigger an extra query per row.
MEMBER_LIST_PREFETCH = (
    "insurances",
    # Addresses, so the eligibility-reason recompute (address_range_reason, shown
    # under a Not Eligible badge) resolves without an extra query per row.
    "addresses",
    # Social care coverage, so the Urgent Care coverage gate
    # (has_valid_social_care / can_request_verification) resolves without an
    # extra query per row.
    "social_care_coverages",
    "military_profile",
    Prefetch(
        "enrollments",
        queryset=EnrollmentVerification.objects.select_related(
            "requested_by", "verified_by"
        ),
    ),
    "member_profiles",
    # The serializer's authorization_status/authorization_status_at read the
    # client's Internal Service case via ``client.cases.all()`` (internal_service_case
    # / primary_case). Prefetch here so the whole page costs ONE cases query
    # instead of one-or-more per row (N+1).
    "cases",
    # Roster of the client's household so the serializer can resolve the primary
    # member's client_id (household_primary_id) without an extra query per row.
    "household_membership__household__members",
    # Household-level enrollments, so the Meals/Boxes kind resolves for a
    # dependent who has no enrollment of their own (kind is household-wide).
    "household_membership__household__enrollment_verifications",
)


def require_internal_service_primary(qs):
    """Restrict a Client queryset to the members the Verification page should
    show: everyone must belong to a household whose PRIMARY member holds an
    Internal Service case (the case the verification + meal/box delivery attach
    to). The internal-service-case holder is always the household primary, so
    dependents are kept via their household and strays with no household — or
    whose primary has no internal-service case — are dropped.

    Caller is responsible for ``.distinct()`` (this adds multi-valued joins)."""
    return qs.filter(
        household_membership__household__members__is_primary=True,
        household_membership__household__members__client__cases__case_type=(
            CaseType.INTERNAL_SERVICE
        ),
    )


def verification_completed_q():
    """Clients whose verification POP-UP was completed: a governing enrollment --
    their own or their household's -- has ``verified_at`` set. DB-level mirror of
    ``lifecycle.verification_completed`` and the single determinant for the
    Verification page's Pending vs Verified split.

    Keyed off the explicit verification fact, NOT the enrollment stage or the
    client's lifecycle_stage. The case authorization status (a separate
    dimension) never affects this.

    Caller is responsible for ``.distinct()`` (this adds multi-valued joins)."""
    return Q(enrollments__verified_at__isnull=False) | Q(
        household_membership__household__enrollment_verifications__verified_at__isnull=False
    )


def verification_scope_q():
    """Base scope for the Verification page, keyed off the verification FACT so
    the list reads as a full verification history: households still awaiting
    verification (lifecycle ``pending_verification``) OR that have EVER been
    verified (``verified_at`` set on a governing enrollment) -- kept even after
    they advance to kitchen assignment / active / completed. The ``verified_at``
    join is multi-valued, so the caller must ``.distinct()``."""
    return Q(lifecycle_stage="pending_verification") | verification_completed_q()


def enrollment_stage_q(*stages):
    """Match a client whose OWN enrollment OR their household's governing
    enrollment sits at any of the given stage(s). Mirrors the On Hold pattern so
    a dependent inherits the household enrollment's stage. Caller handles
    ``.distinct()`` (this adds multi-valued joins)."""
    return Q(enrollments__stage__in=stages) | Q(
        household_membership__household__enrollment_verifications__stage__in=stages
    )


def governing_enrollment_stage():
    """A ``Subquery`` yielding the stage of the client's GOVERNING enrollment --
    the SQL mirror of ``serializers.active_enrollment`` (most-recent non-
    disregarded enrollment, preferring an OPEN one, with a household fallback).

    Stage-based status filters (On Hold / Open / Closed) must key off this ONE
    governing enrollment, the same way the rest of the app does -- NOT off "any
    enrollment at this stage" (``enrollment_stage_q``), which over-matches a
    member who has a stray extra live enrollment (e.g. an On Hold enrollment
    alongside a newer Service Active one). Validated to match ``active_enrollment``
    exactly on the production snapshot."""
    def _latest(rel):
        return Subquery(
            EnrollmentVerification.objects
            .filter(**rel)
            .exclude(stage=EnrollmentStage.DISREGARDED)
            .annotate(_open=SQLCase(
                When(closed_at__isnull=True, then=Value(1)),
                default=Value(0), output_field=IntegerField(),
            ))
            .order_by("-_open", "-opened_at")
            .values("stage")[:1]
        )
    # Own enrollments take precedence; a dependent with none inherits the
    # household's governing enrollment.
    return Coalesce(
        _latest({"client": OuterRef("pk")}),
        _latest({"household__members__client": OuterRef("pk")}),
    )


def governing_auth_expired_q():
    """Match a client whose GOVERNING internal-service case authorization has
    EXPIRED (``service_authorization_status`` = expired). Anchored on the
    internal-service (meal/box) case via ``Exists`` so it stays scoped to the
    case that actually governs. Caller handles ``.distinct()``."""
    internal_cases = Case.objects.filter(
        client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
    )
    return Exists(
        internal_cases.filter(
            service_authorization_status=ServiceAuthorizationStatus.EXPIRED
        )
    )


_ALLERGY_LABELS = dict(FoodAllergy.choices)


def _allergy_labels(codes):
    """Human labels for a member's food-allergy codes, dropping the no-op 'none'."""
    return [_ALLERGY_LABELS.get(c, c) for c in (codes or []) if c and c != "none"]


def predict_member_out_of_orbit(profile):
    """Predict whether a member will be Out of Orbit once a kitchen is assigned,
    from the GLOBAL meal rule + data completeness. Kitchen-agnostic (no kitchen
    is assigned yet at the Logistics stage). Returns ``(out: bool, reason: str)``.

    A member is predicted Out of Orbit when they have no menu type yet, or when
    their menu type + food allergies can't be safely fulfilled by the meal rule
    (see api.services.meal_rules). Kitchen-coverage is a separate, household-level
    check (whether ANY kitchen can serve everyone)."""
    if profile is None or not (profile.menu_type or "").strip():
        return True, "No menu type assigned"
    rule = resolve_kitchen_meal(profile.menu_type, profile.food_allergies)
    if rule.out_of_orbit:
        return True, "Menu + allergies can't be safely fulfilled"
    return False, ""


def _parse_date(value):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def period_date_range(period):
    """Map a Verification-page period code to an inclusive (start, end) date
    window on the LOCAL calendar, or None for "all"/blank/unknown (no filter).

    Weeks start on Monday. Current periods (this_week/month/year) end today
    rather than at the calendar boundary -- records can't be in the future, so
    this is equivalent and avoids surprising empty ranges.
    """
    period = (period or "").strip().lower()
    if not period or period == "all":
        return None
    today = timezone.localdate()
    if period == "today":
        return today, today
    if period == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if period == "this_week":
        return today - timedelta(days=today.weekday()), today
    if period == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        return this_week_start - timedelta(days=7), this_week_start - timedelta(days=1)
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == "this_year":
        return today.replace(month=1, day=1), today
    return None


def apply_period_filter(qs, period):
    """Restrict ``qs`` to clients whose governing enrollment -- their own or
    their household's -- was OPENED within the period window. No-op when the
    period maps to no range. Caller is responsible for ``.distinct()`` (this
    adds multi-valued joins)."""
    rng = period_date_range(period)
    if not rng:
        return qs
    start, end = rng
    return qs.filter(
        Q(
            enrollments__opened_at__date__gte=start,
            enrollments__opened_at__date__lte=end,
        )
        | Q(
            household_membership__household__enrollment_verifications__opened_at__date__gte=start,
            household_membership__household__enrollment_verifications__opened_at__date__lte=end,
        )
    )


def apply_enrollment_date_filter(qs, field, start, end):
    """Restrict ``qs`` to clients whose governing enrollment -- their own or
    their household's -- has the datetime ``field`` (e.g. ``opened_at`` for when
    the verification was requested, ``verified_at`` for when it was completed)
    within the inclusive [start, end] date window. Either bound may be None
    (open-ended). No-op when both bounds are None. The conditions for a bound
    are ANDed on the SAME joined row (matching ``apply_period_filter``); the
    caller is responsible for ``.distinct()`` (this adds multi-valued joins)."""
    if not start and not end:
        return qs
    hh = "household_membership__household__enrollment_verifications__"
    own_cond, hh_cond = {}, {}
    if start:
        own_cond[f"enrollments__{field}__date__gte"] = start
        hh_cond[f"{hh}{field}__date__gte"] = start
    if end:
        own_cond[f"enrollments__{field}__date__lte"] = end
        hh_cond[f"{hh}{field}__date__lte"] = end
    return qs.filter(Q(**own_cond) | Q(**hh_cond))


def apply_authorization_date_filter(qs, start, end):
    """Restrict ``qs`` to clients whose GOVERNING internal-service case has an
    authorization approval-start date (``service_authorization_approval_starts_at``
    -- i.e. when the case was authorized) within the inclusive [start, end]
    window. Either bound may be None (open-ended); no-op when both are None.

    Uses ``Exists`` on the internal-service case (mirroring
    ``apply_authorization_filter``), so it introduces no join duplicates and
    needs no ``.distinct()`` of its own. As with the authorization status
    filter, the case is held by the household primary, so a matching primary
    brings its household into the grouped result."""
    if not start and not end:
        return qs
    cases = Case.objects.filter(
        client=OuterRef("pk"),
        case_type=CaseType.INTERNAL_SERVICE,
    )
    if start:
        cases = cases.filter(service_authorization_approval_starts_at__date__gte=start)
    if end:
        cases = cases.filter(service_authorization_approval_starts_at__date__lte=end)
    return qs.filter(Exists(cases))


def apply_case_created_date_filter(qs, start, end):
    """Restrict ``qs`` to clients whose GOVERNING internal-service case was
    CREATED/opened (``Case.date_opened``) within the inclusive [start, end]
    window. Either bound may be None (open-ended); no-op when both are None.

    Uses ``Exists`` on the internal-service case (mirroring
    ``apply_authorization_date_filter``), so it introduces no join duplicates
    and needs no ``.distinct()`` of its own."""
    if not start and not end:
        return qs
    cases = Case.objects.filter(
        client=OuterRef("pk"),
        case_type=CaseType.INTERNAL_SERVICE,
    )
    if start:
        cases = cases.filter(date_opened__date__gte=start)
    if end:
        cases = cases.filter(date_opened__date__lte=end)
    return qs.filter(Exists(cases))


def apply_verification_date_filters(qs, params):
    """Apply the Verification-page case-created/requested/completed/authorized
    date-range filters from query params (``case_from``/``case_to`` ->
    internal-service case ``date_opened``; ``requested_from``/``requested_to``
    -> enrollment ``opened_at``; ``completed_from``/``completed_to`` ->
    enrollment ``verified_at``; ``authorized_from``/``authorized_to`` ->
    internal-service case ``service_authorization_approval_starts_at``). Returns
    (qs, changed) where ``changed`` signals the caller to ``.distinct()``."""
    changed = False
    case_from, case_to = _parse_date(params.get("case_from")), _parse_date(params.get("case_to"))
    if case_from or case_to:
        qs = apply_case_created_date_filter(qs, case_from, case_to)
        # Exists-based, so no distinct is required for this bound alone.
    req_from, req_to = _parse_date(params.get("requested_from")), _parse_date(params.get("requested_to"))
    if req_from or req_to:
        qs = apply_enrollment_date_filter(qs, "opened_at", req_from, req_to)
        changed = True
    comp_from, comp_to = _parse_date(params.get("completed_from")), _parse_date(params.get("completed_to"))
    if comp_from or comp_to:
        qs = apply_enrollment_date_filter(qs, "verified_at", comp_from, comp_to)
        changed = True
    auth_from, auth_to = _parse_date(params.get("authorized_from")), _parse_date(params.get("authorized_to"))
    if auth_from or auth_to:
        qs = apply_authorization_date_filter(qs, auth_from, auth_to)
        # Exists-based, so no distinct is required for this bound alone; but the
        # requested/completed joins above may still need it.
    return qs, changed


class MenuTypesListView(PortalAPIView):
    """Active menu types for the Members-page menu-type filter dropdown.
    ``value`` matches ``MemberDietaryProfile.menu_type`` (the catalog name)."""

    def get(self, request):
        rows = MenuType.objects.filter(is_active=True).order_by("name")
        return Response([{"value": mt.name, "label": mt.name} for mt in rows])


class FoodAllergiesListView(PortalAPIView):
    """Food-allergy options for the Members-page allergy filter dropdown, built
    from the FoodAllergy enum so the list stays in sync with the backend. The
    no-op 'none' sentinel is dropped (nothing to filter on). ``value`` is the
    enum code; the list filter matches both the code and its label because the
    stored ``food_allergies`` data is mixed (mostly codes, some labels)."""

    def get(self, request):
        return Response([
            {"value": code, "label": label}
            for code, label in FoodAllergy.choices
            if code != "none"
        ])


class LeadSourcesListView(PortalAPIView):
    """Lead-source options for the Members-page filter dropdown.

    Mirrors the extension's Lead Source picker, which is populated from the
    CallTools queues (``value`` = queue id). Merged with any distinct
    ``Client.lead_source`` values already stored (so legacy/free-text values
    such as "Williamsburg" stay filterable even when they aren't a queue id).
    """

    def get(self, request):
        options = []
        # De-dupe by LABEL (case-insensitive) so a CallTools queue and a stored
        # free-text value that share a name (e.g. the "Williamsburg" queue id
        # 5975 vs. the stored value "Williamsburg") collapse to ONE option.
        seen_labels = set()

        # Stored Client.lead_source values FIRST: these are what the filter
        # actually matches (``lead_source__iexact=value``), so when a label
        # collides we keep the stored value and drop the queue-id twin. Ordering
        # is cleared so .distinct() collapses properly (the model's Meta.ordering
        # would otherwise leak into SELECT DISTINCT and return duplicates).
        stored = (
            Client.objects.exclude(lead_source="")
            .order_by()
            .values_list("lead_source", flat=True)
            .distinct()
        )
        for val in stored:
            v = (val or "").strip()
            key = v.lower()
            if not v or key in seen_labels:
                continue
            seen_labels.add(key)
            options.append({"value": v, "label": v})

        # CallTools QUEUES + CAMPAIGNS -- the same sources the extension's Lead
        # Source picker uses. Skip any whose name already exists as a stored
        # value (label collision). Simple label-merge: queue/campaign ids share
        # no namespace, but agents pick by label so collisions are acceptable.
        try:
            from ..integrations.calltools import campaigns as ct_campaigns
            from ..integrations.calltools import config as ct_config
            from ..integrations.calltools import queues as ct_queues

            if ct_config.is_enabled():
                ct_options = []
                try:
                    ct_options += ct_queues.list_queue_options()
                except Exception:
                    pass
                try:
                    ct_options += ct_campaigns.list_campaign_options()
                except Exception:
                    pass
                for q in ct_options:
                    # Store/filter by NAME (free text) to match what the ext now
                    # saves into Client.lead_source -- the queue/campaign name,
                    # not its CallTools id.
                    name = (q.get("name") or "").strip()
                    if not name or name.lower() in seen_labels:
                        continue
                    seen_labels.add(name.lower())
                    label = name if q.get("active", True) else f"{name} (inactive)"
                    options.append({"value": name, "label": label})
        except Exception:  # never let a CallTools hiccup break the filter
            pass

        options.sort(key=lambda o: (o["label"] or "").lower())
        return Response(options)


class TeamsListView(PortalAPIView):
    """Team options for the Members-page Team filter dropdown.

    Distinct CareCircle originating teams across the Unite Us agents (the team
    each case creator belongs to). ``value`` == ``label`` == the stored
    ``UniteUsAgent.originating_team`` string the filter matches
    (``team__iexact=value``)."""

    # "Met Council Team" is the default originating_team assigned to everyone
    # not on the CareCircle roster (i.e. non-US staff), so it isn't a real
    # filterable CareCircle team -- hide it from the Team filter dropdown.
    _EXCLUDED_TEAMS = {"met council team"}

    def get(self, request):
        teams = (
            UniteUsAgent.objects.exclude(originating_team="")
            .order_by()
            .values_list("originating_team", flat=True)
            .distinct()
        )
        options = sorted({
            (t or "").strip()
            for t in teams
            if (t or "").strip()
            and (t or "").strip().lower() not in self._EXCLUDED_TEAMS
        })
        return Response([{"value": t, "label": t} for t in options])


class MembersListView(PortalGenericAPIView):
    serializer_class = s.MemberListSerializer

    def get_queryset(self):
        qs = (
            Client.objects.all()
            .select_related("household_membership__household")
            .prefetch_related(*MEMBER_LIST_PREFETCH)
        )
        params = self.request.query_params

        search = (params.get("search") or "").strip()
        if search:
            cond = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(insurances__external_member_id__icontains=search)
            )
            # Multi-word "first last" search.
            parts = search.split()
            if len(parts) >= 2:
                cond |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
            dob = _parse_date(search)
            if dob:
                cond |= Q(date_of_birth=dob)
            try:
                cond |= Q(client_id=uuid.UUID(search))
            except (ValueError, TypeError, AttributeError):
                pass
            qs = qs.filter(cond)

        # Page-level scope (Verification / Logistics) restricts which members are
        # ever shown, before the per-status filter chips are applied.
        scope = (params.get("scope") or "").strip()
        if scope == "verification":
            # Full verification history: the pending queue + anything ever
            # verified, regardless of the stage it later advanced to. Also
            # restrict to members whose household primary holds an Internal
            # Service case (see require_internal_service_primary).
            qs = require_internal_service_primary(qs.filter(verification_scope_q()))
        elif scope == "need_attention":
            # "Need Attention" (Urgent Care): brand-new members whose first
            # internal-service case was created by the ext (Client.is_new) and who
            # have NOT yet entered the verification pipeline at all. Exclude anyone
            # who already has ANY enrollment -- their own OR their household's -- in
            # any stage (pending verification, verified-or-beyond, on hold,
            # cancelled, disregarded): the existence of an enrollment means a
            # verification was already requested/handled, so a stale is_new flag
            # must never keep them on the list. Leaves only members with no
            # enrollment yet.
            qs = qs.filter(is_new=True).exclude(
                Q(enrollments__isnull=False)
                | Q(household_membership__household__enrollment_verifications__isnull=False)
            ).distinct()
            # Optional case-created date-range filter (Urgent Care triage): keep
            # only members whose governing internal-service case was OPENED within
            # [case_from, case_to]. Matched against the internal-service case's
            # date_opened so it lines up with the "Created" column shown on the
            # page. Either bound may be omitted for an open-ended range.
            case_from = _parse_date(params.get("case_from"))
            case_to = _parse_date(params.get("case_to"))
            if case_from or case_to:
                date_q = Q()
                if case_from:
                    date_q &= Q(cases__date_opened__date__gte=case_from)
                if case_to:
                    date_q &= Q(cases__date_opened__date__lte=case_to)
                qs = qs.filter(
                    Q(cases__case_type=CaseType.INTERNAL_SERVICE) & date_q
                ).distinct()
        else:
            scope_stages = SCOPE_TO_STAGES.get(scope)
            if scope_stages:
                qs = qs.filter(lifecycle_stage__in=scope_stages)

        # Logistics (kitchen-assignment) page: drop members whose internal-
        # service (meal/box) cases are ALL closed/cancelled -- they've finished
        # service and shouldn't wait for a kitchen. Kept if ANY internal-service
        # case is still open (blank/unknown status counts as open, so we never
        # over-hide). The case is held by the household primary, so a household
        # drops out once the primary's case is done; dependents follow via the
        # roster build.
        if scope == "logistics":
            open_internal_case = (
                Case.objects.filter(
                    client=OuterRef("pk"),
                    case_type=CaseType.INTERNAL_SERVICE,
                )
                .exclude(
                    case_status__in=(CaseStatus.CLOSED, CaseStatus.CANCELLED)
                )
            )
            qs = qs.filter(Exists(open_internal_case))
            # A paused (On Hold) household keeps lifecycle_stage=kitchen_assignment
            # -- the hold is an overlay on the underlying stage -- so exclude it
            # here to actually remove it from the kitchen-assignment queue (e.g.
            # a sole internal-service case that was denied auto-pauses the member).
            qs = qs.exclude(
                Q(enrollments__stage=EnrollmentStage.ON_HOLD)
                | Q(
                    household_membership__household__enrollment_verifications__stage=(
                        EnrollmentStage.ON_HOLD
                    )
                )
            )

        # Status filter (Members page grouped dropdown). One selection spanning
        # several axes; each value maps to exactly one query. Legacy Verification
        # page values (Pending / Verified / Denied / Kitchen Assignment / ...) are
        # kept for backward-compat alongside the new grouped values.
        status_val = (params.get("status") or "").strip()
        sv = status_val.lower()
        if status_val and sv != "all":
            # ── Eligibility axis (client eligibility gate) ──
            if sv == "eligible":
                # Passed the gate: valid Medicaid + in-range ZIP/state. Anyone NOT
                # parked at the hard Ineligible off-ramp, at any later stage.
                qs = qs.exclude(lifecycle_stage=ClientStage.INELIGIBLE)
            elif sv == "ineligible":
                # Failed the gate: missing/expired Medicaid, wrong Medicaid type,
                # or out-of-range ZIP/state (set by reconcile_client_eligibility).
                qs = qs.filter(lifecycle_stage=ClientStage.INELIGIBLE)
            # ── legacy eligibility-denial value (Verification page "Denied") ──
            elif status_val in ("Denied", "not_eligible"):
                qs = qs.filter(lifecycle_stage="not_eligible")
            # ── Verification axis (yes/no fact) ──
            elif status_val in ("verified_awaiting", "Verified", "verified"):
                # Pop-up completed (verified_at set). Independent of case auth.
                qs = qs.filter(verification_completed_q())
            elif status_val in ("pending_verification", "Pending"):
                # Pop-up NOT yet completed (verified_at null) AND the member is
                # actually IN the verification window -- mirrors the Verification
                # column (serializers.get_verification_state), which only labels a
                # member "Pending Verification" at these stages and shows blank
                # otherwise. Without the stage gate, the Members page (no scope)
                # would also match Inactive/Screened members who never entered
                # verification, surfacing rows with a blank Verification column.
                qs = qs.filter(
                    lifecycle_stage__in=[
                        "pending_verification", "verified", "kitchen_assignment",
                    ]
                ).exclude(verification_completed_q())
            # ── Authorization axis (governing internal-service case) ──
            elif sv in ("auth_pending", "waiting_authorization"):
                qs = apply_authorization_filter(qs, "pending")
            elif sv == "authorized":
                qs = apply_authorization_filter(qs, "approved")
            elif sv == "auth_denied":
                qs = apply_authorization_filter(qs, "denied")
            # ── Service axis ──
            elif status_val in ("On Hold", "on_hold"):
                # On Hold is a PROGRAM (enrollment) state -- scope to the member's
                # GOVERNING enrollment (not "any enrollment"), so a stray On Hold
                # enrollment alongside a newer live one doesn't misfile them.
                qs = qs.annotate(_gov_stage=governing_enrollment_stage()).filter(
                    _gov_stage=EnrollmentStage.ON_HOLD
                )
            elif sv == "out_of_range":
                qs = qs.filter(current_member_status_exists(MemberStatus.OUT_OF_RANGE))
            # ── Terminal axis (program / enrollment stage) ── all keyed off the
            # GOVERNING enrollment (mirrors active_enrollment), never "any".
            elif sv == "term_open":
                # Open program: the governing enrollment is actively serving.
                qs = qs.annotate(_gov_stage=governing_enrollment_stage()).filter(
                    _gov_stage=EnrollmentStage.SERVICE_ACTIVE
                )
            elif sv == "term_expired":
                # Authorization window/status expired on the governing case.
                qs = qs.filter(governing_auth_expired_q())
            elif sv == "term_closed":
                # Closed/off-boarded program: the GOVERNING enrollment is terminal
                # (closed / cancelled / service-complete). Keyed off the governing
                # enrollment so a member actively served on a live enrollment isn't
                # shown as Closed just because an old superseded enrollment is.
                qs = qs.annotate(_gov_stage=governing_enrollment_stage()).filter(
                    _gov_stage__in=[
                        EnrollmentStage.CLOSED,
                        EnrollmentStage.CANCELLED,
                        EnrollmentStage.SERVICE_COMPLETE,
                    ]
                )
            # ── Logistics / plain lifecycle-stage buckets ──
            elif sv == "kitchen_assignment":
                qs = qs.filter(lifecycle_stage="kitchen_assignment")
            elif sv == "active":
                qs = qs.filter(lifecycle_stage="active")
            else:
                stages = STATUS_TO_STAGES.get(status_val)
                if stages:
                    qs = qs.filter(lifecycle_stage__in=stages)
                else:
                    qs = qs.filter(lifecycle_stage=status_val)

        # Eligibility filter (its own dimension, composes with the status chips):
        # whether the member cleared the hard eligibility gate. "eligible" = NOT
        # parked on the INELIGIBLE off-ramp; "ineligible" = on it. Mirrors the
        # serializer's `eligibility` field and the Eligible column.
        elig_val = (params.get("eligibility") or "").strip().lower()
        if elig_val == "eligible":
            qs = qs.exclude(lifecycle_stage=ClientStage.INELIGIBLE)
        elif elig_val == "ineligible":
            qs = qs.filter(lifecycle_stage=ClientStage.INELIGIBLE)

        # Authorization filter (separate dimension from verification): match the
        # client's GOVERNING internal-service case authorization. Composes with
        # the status chips.
        auth_val = (params.get("authorization") or "").strip().lower()
        if auth_val in AUTHORIZATION_FILTERS:
            qs = apply_authorization_filter(qs, auth_val)

        # Internal-service filter: only members who hold an Internal Service
        # case (the meal/box case the verification + delivery attach to; in our
        # data this is the household primary). Independent of the status chips,
        # so it composes with "All" or any verification status.
        if (params.get("has_internal_service") or "").strip().lower() in (
            "1", "true", "yes",
        ):
            qs = qs.filter(cases__case_type=CaseType.INTERNAL_SERVICE)
            # Open/closed sub-filter: ONLY active when Internal Service is on
            # (it narrows that set). Mirrors the "current case" the Created column
            # shows: a member is OPEN if they have any non-terminal internal-
            # service case (actively serviced), else CLOSED. So "closed" means the
            # member has NO open internal-service case (all done) -- NOT merely
            # "has a closed case", since members with an open case also usually
            # have older closed ones and would otherwise wrongly show as closed.
            internal_status = (params.get("internal_status") or "").strip().lower()
            if internal_status in ("open", "closed"):
                terminal = (CaseStatus.CLOSED, CaseStatus.CANCELLED)
                open_case = Case.objects.filter(
                    client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
                ).exclude(case_status__in=terminal)
                if internal_status == "open":
                    qs = qs.filter(Exists(open_case))
                else:
                    qs = qs.exclude(Exists(open_case))

        # Product-kind filter (Meals vs Boxes), keyed off the household's program
        # name. A household is always one kind, so meals/boxes never mix.
        service_type = (params.get("service_type") or "").strip().lower()
        kw = {"meals": "meal", "boxes": "box"}.get(service_type)
        if kw:
            qs = qs.filter(
                Q(enrollments__program_name__icontains=kw)
                | Q(
                    household_membership__household__enrollment_verifications__program_name__icontains=kw
                )
            )

        # Kitchen filter: the member's (or their household's) enrollment kitchen.
        kitchen_id = (params.get("kitchen") or "").strip()
        if kitchen_id:
            qs = qs.filter(
                Q(enrollments__kitchen_id=kitchen_id)
                | Q(
                    household_membership__household__enrollment_verifications__kitchen_id=kitchen_id
                )
            )

        # Lead-source filter (Members page): the client's stored lead source
        # (a CallTools queue id, or legacy free-text such as "Williamsburg").
        # ``lead_source`` is a direct Client column, so no extra join/distinct.
        # Exact, case-insensitive match on the value chosen from the dropdown.
        lead_source_val = (params.get("lead_source") or "").strip()
        if lead_source_val:
            qs = qs.filter(lead_source__iexact=lead_source_val)

        # Special status flags that aren't lifecycle stages:
        #   * out_of_orbit -> the member has a MemberDietaryProfile the meal rule
        #     couldn't safely fulfill (status OUT_OF_ORBIT).
        #   * on_hold      -> the member's (or household's) enrollment is paused
        #     (On Hold). NB: lifecycle_stage keeps the held-from stage, so this
        #     must be filtered on the enrollment stage, not lifecycle_stage.
        flag = (params.get("flag") or "").strip().lower()
        # The `flag` filter is for INDIVIDUAL member-level statuses only. Program-
        # level states (On Hold / Cancelled) live on the grouped status filter, not
        # here, and are intentionally NOT member flags.
        if flag == "out_of_orbit":
            qs = qs.filter(current_member_status_exists(MemberStatus.OUT_OF_ORBIT))
        elif flag == "out_of_range":
            qs = qs.filter(current_member_status_exists(MemberStatus.OUT_OF_RANGE))
        elif flag == "paused":
            # AGENT (manual) pause only -- exclude the eligibility-driven pause so
            # the two are distinguishable on the list.
            qs = qs.filter(current_member_status_exists(
                MemberStatus.PAUSED, eligibility_paused=False,
            ))
        elif flag == "eligibility_paused":
            # AUTO pause: the member failed their own eligibility (expired
            # insurance / missing coverage).
            qs = qs.filter(current_member_status_exists(
                MemberStatus.PAUSED, eligibility_paused=True,
            ))
        # TEMP diagnostic flags (to be removed): members missing dietary/logistics
        # data. "no_menu_type" -> no dietary profile carries a menu type at all;
        # "no_kitchen" -> neither the member's nor their household's enrollment has
        # a kitchen assigned.
        elif flag == "no_menu_type":
            qs = qs.exclude(member_profiles__menu_type__gt="")
        elif flag == "no_kitchen":
            qs = qs.exclude(
                Q(enrollments__kitchen_id__isnull=False)
                | Q(
                    household_membership__household__enrollment_verifications__kitchen_id__isnull=False
                )
            )

        # Household-composition filter:
        #   "multi"  -> members whose household has more than one member.
        #   "single" -> members in a solo (one-member) household OR ungrouped
        #               individuals with no household (member count <= 1).
        household_filter = (params.get("household") or "").strip().lower()
        if household_filter in ("multi", "single"):
            qs = qs.annotate(
                _hh_member_count=Count(
                    "household_membership__household__members", distinct=True
                )
            )
            if household_filter == "multi":
                qs = qs.filter(_hh_member_count__gt=1)
            else:  # single
                qs = qs.filter(_hh_member_count__lte=1)

        # Program-type (scope) filter — its own dimension, meant to be combined
        # with the household-composition filter above to surface data mismatches
        # (e.g. an INDIVIDUAL-scope program that nonetheless has 2+ members, or a
        # HOUSEHOLD-scope program with a single member). Scope is derived LIVE
        # from the governing program name -- the source of truth, mirroring
        # derive_household_type / the Case serializer: a program is HOUSEHOLD when
        # "household" appears in its name, else INDIVIDUAL. Matched on the member's
        # own enrollment OR their household's enrollment so dependents inherit the
        # household's program.
        program_type = (params.get("program_type") or "").strip().lower()
        if program_type in ("household", "individual"):
            household_prog = (
                Q(enrollments__program_name__icontains="household")
                | Q(
                    household_membership__household__enrollment_verifications__program_name__icontains="household"
                )
            )
            has_prog = (
                Q(enrollments__isnull=False)
                | Q(
                    household_membership__household__enrollment_verifications__isnull=False
                )
            )
            if program_type == "household":
                qs = qs.filter(household_prog)
            else:  # individual: has a program, but none of household scope
                qs = qs.filter(has_prog).exclude(household_prog)

        # Menu-type filter (Members page): the member's assigned catalog menu
        # type. MemberDietaryProfile.menu_type stores the catalog NAME, so match
        # on the name passed from the dropdown.
        menu_type_val = (params.get("menu_type") or "").strip()
        if menu_type_val and menu_type_val.lower() != "all":
            qs = qs.filter(member_profiles__menu_type=menu_type_val)

        # Allergy filter (Members page, multi-select): keep members whose dietary
        # profile lists EVERY selected allergy (ALL/AND semantics). Values arrive
        # as a comma-separated list of FoodAllergy codes (or repeated ?allergy=).
        # ``food_allergies`` is a Postgres JSONField, so use the ``contains``
        # lookup; because the stored data is mixed (mostly codes, a few labels)
        # each selected code matches EITHER its code OR its label. Chaining one
        # ``filter`` per allergy ANDs them together.
        allergy_vals = [v.strip() for v in params.getlist("allergy") if v.strip()]
        if not allergy_vals:
            allergy_vals = [
                v.strip() for v in (params.get("allergy") or "").split(",") if v.strip()
            ]
        if allergy_vals:
            allergy_labels = dict(FoodAllergy.choices)
            for code in allergy_vals:
                label = allergy_labels.get(code, code)
                qs = qs.filter(
                    Q(member_profiles__food_allergies__contains=[code])
                    | Q(member_profiles__food_allergies__contains=[label])
                )

        # Team filter (Members page): keep members whose INTERNAL-SERVICE case
        # was CREATED by a Unite Us agent on the selected CareCircle originating
        # team. Case.created_by_id == UniteUsAgent.user_id, so resolve the team's
        # agent user_ids first, then match the case creator. Composes with the
        # other filters; the trailing .distinct() dedupes the case join.
        team_val = (params.get("team") or "").strip()
        if team_val:
            team_creator_ids = list(
                UniteUsAgent.objects.filter(originating_team__iexact=team_val)
                .values_list("user_id", flat=True)
            )
            qs = qs.filter(
                cases__case_type=CaseType.INTERNAL_SERVICE,
                cases__created_by_id__in=team_creator_ids,
            )

        # Created-date range filter (Members page): filters on the date the
        # member's INTERNAL-SERVICE case was created (its ``date_opened``) --
        # NOT the Client record's own ``created_at``. Mirrors the Urgent Care
        # triage filter above and the ``case_created_at`` column, so the range
        # searches the internal-service case creation date. Both bounds are
        # applied to the SAME internal-service case row (one .filter() over the
        # multi-valued relation); the trailing .distinct() dedupes the join.
        # Inclusive [from, to]; either bound may be omitted.
        created_from = _parse_date(params.get("created_from"))
        created_to = _parse_date(params.get("created_to"))
        if created_from or created_to:
            case_date_q = Q(cases__case_type=CaseType.INTERNAL_SERVICE)
            if created_from:
                case_date_q &= Q(cases__date_opened__date__gte=created_from)
            if created_to:
                case_date_q &= Q(cases__date_opened__date__lte=created_to)
            qs = qs.filter(case_date_q)

        # Closed-date range filter (Members page): filters on the date the
        # member's INTERNAL-SERVICE case was CLOSED (its ``case_closed_at``, the
        # C: date shown in the "Created" column). Mirrors the created-date filter
        # above but on the close date, so an ops user can pull members whose
        # service case was closed within a window (e.g. "closed today") and then
        # cross-check paused/active status via the status filter. Both bounds
        # apply to the SAME internal-service case row; .distinct() dedupes the
        # join. Inclusive [from, to]; either bound may be omitted.
        closed_from = _parse_date(params.get("closed_from"))
        closed_to = _parse_date(params.get("closed_to"))
        if closed_from or closed_to:
            case_closed_q = Q(cases__case_type=CaseType.INTERNAL_SERVICE)
            if closed_from:
                case_closed_q &= Q(cases__case_closed_at__date__gte=closed_from)
            if closed_to:
                case_closed_q &= Q(cases__case_closed_at__date__lte=closed_to)
            qs = qs.filter(case_closed_q)

        # Date-period filter (Verification page dropdown): narrow to households
        # whose enrollment record was OPENED within the selected window.
        qs = apply_period_filter(qs, params.get("period"))

        # Verification page requested/completed date-range filters (from/to on
        # the enrollment's opened_at / verified_at respectively).
        qs, _ = apply_verification_date_filters(qs, params)

        return qs.distinct()

    def _serialize_member(self, client, is_primary, relationship=""):
        data = s.MemberListSerializer(client).data
        data["is_primary"] = is_primary
        data["relationship"] = relationship
        data["service_type"] = self._service_type_for_client(client)
        return data

    # Human labels for the creation source recorded on each record's first
    # history row (see api.history.ChangeSource).
    _ADDED_VIA_LABELS = {
        "extension": "Extension",
        "import": "Import",
        "admin": "Admin",
        "crm": "CRM",
        "system": "System",
    }

    def _added_via_map(self, ids):
        """{client_id(str): (source_code, label)} for how each client was first
        created, from the EARLIEST historical row (history_type='+') of the
        Client, whose ``change_source`` records whether the extension, the CSV /
        Unite Us import, admin, etc. created the record. Batched into ONE query
        over the given client ids to avoid an N+1. Blank source (e.g. records
        that predate change-source stamping) surfaces as 'Unknown'."""
        result = {}
        if not ids:
            return result
        hist_model = Client.history.model
        src_by_id = {}
        for cid, src in (
            hist_model.objects.filter(history_type="+", client_id__in=ids)
            .order_by("history_date")
            .values_list("client_id", "change_source")
        ):
            key = str(cid)
            # First create row wins (ordered oldest-first).
            if key not in src_by_id:
                src_by_id[key] = src or ""
        for key, src in src_by_id.items():
            result[key] = (
                src,
                self._ADDED_VIA_LABELS.get(
                    src, src.replace("_", " ").title() if src else "Unknown"
                ),
            )
        return result

    def _case_team_map(self, page):
        """{client_id(str): originating_team} for the page -- the CareCircle team
        of the Unite Us agent who CREATED each member's governing internal-service
        case (Case.created_by_id == UniteUsAgent.user_id). Batched into ONE
        UniteUsAgent query over the page's distinct creator ids to avoid an N+1.
        Blank when the case has no known creator or the creator isn't a known
        Unite Us agent."""
        creator_by_client = {}
        creator_ids = set()
        for c in page:
            case = s.internal_service_case(c)
            if case is not None and case.created_by_id:
                creator_by_client[str(c.client_id)] = str(case.created_by_id).lower()
                creator_ids.add(case.created_by_id)
        team_by_uid = {}
        if creator_ids:
            team_by_uid = {
                str(uid).lower(): (team or "")
                for uid, team in UniteUsAgent.objects.filter(
                    user_id__in=creator_ids
                ).values_list("user_id", "originating_team")
            }
        return {
            cid: team_by_uid.get(uid, "")
            for cid, uid in creator_by_client.items()
        }

    def _stamp_added_via(self, groups):
        """Annotate each member dict in ``groups`` with how the client was first
        created: ``added_via`` (raw ChangeSource code) + ``added_via_label``."""
        ids = {
            m["id"]
            for g in groups
            for m in g.get("members", [])
        }
        via = self._added_via_map(ids)
        for g in groups:
            for m in g.get("members", []):
                src, label = via.get(m["id"], ("", "Unknown"))
                m["added_via"] = src
                m["added_via_label"] = label

    def _stamp_case_teams(self, groups):
        """Annotate each member dict in ``groups`` with ``case_created_by_team``
        -- the CareCircle originating team of their internal-service case creator
        (see :meth:`_case_team_map`). Batched: ONE query to reload the page's
        clients (with cases) + ONE UniteUsAgent query, so the whole page costs
        two queries rather than an N+1. Used by the Urgent Care list."""
        ids = {
            m["id"]
            for g in groups
            for m in g.get("members", [])
        }
        if not ids:
            return
        clients = Client.objects.filter(client_id__in=ids).prefetch_related("cases")
        team_by_client = self._case_team_map(clients)
        for g in groups:
            for m in g.get("members", []):
                m["case_created_by_team"] = team_by_client.get(m["id"], "")

    @staticmethod
    def _hidden_in_logistics(client):
        """Members that shouldn't wait in the kitchen-assignment queue and so are
        dropped from the Logistics roster: out-of-orbit members (the meal rule
        can't safely fulfill them) and members whose internal-service case(s) are
        ALL closed/cancelled (service finished). A dependent with no internal-
        service case of their own is kept -- they ride with the household."""
        if s.member_out_of_orbit(client):
            return True
        internal = [
            c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE
        ]
        if internal and all(
            c.case_status in (CaseStatus.CLOSED, CaseStatus.CANCELLED) for c in internal
        ):
            return True
        return False

    @staticmethod
    def _service_type_for_client(client):
        """Meals/Boxes kind derived from the client's enrollment program name
        (prefetched), falling back to the household's enrollments so a dependent
        with no enrollment of their own still resolves (the kind is household-
        wide). Empty when neither keyword is present anywhere."""
        for enr in client.enrollments.all():
            kind = product_type_kind_for_name(enr.program_name)
            if kind:
                return kind
        membership = getattr(client, "household_membership", None)
        household = getattr(membership, "household", None) if membership else None
        if household is not None:
            for enr in household.enrollment_verifications.all():
                kind = product_type_kind_for_name(enr.program_name)
                if kind:
                    return kind
        return ""

    def _group_entries(self, sort_field="created", descending=True):
        """Lightweight, ordered list of group identifiers for the filtered set
        WITHOUT serializing anyone. Each entry is
        ``{"type": "household"|"individual", "id", "name"}``. Households are
        de-duplicated and ordered (with individuals) so pagination is stable and
        only the requested page is ever built + serialized. A household is
        included when ANY member matches; its full roster is loaded when the page
        is built.

        ``sort_field`` selects the timestamp the groups are ordered by:
          * ``created``      -> Client.created_at (default; most-recently-added)
          * ``requested``    -> the enrollment's requested_at/opened_at
            (Verification page "Requested" column)
          * ``completed``    -> the enrollment's verified_at ("Completed" column)
          * ``case_created`` -> the member's latest internal-service case
            date_opened (Urgent Care "Case Created" column)
        Timestamps are aggregated as the MAX across a client's enrollments and
        then across a household's matching members. Groups with no timestamp sort
        last regardless of direction; name (case-insensitive) breaks ties."""
        # Latest internal-service case date_opened per client, as a scalar
        # subquery so the multi-valued cases relation can't multiply rows (it
        # matches the "Case Created" column the Urgent Care list renders).
        latest_case_opened = (
            Case.objects.filter(
                client=OuterRef("pk"),
                case_type=CaseType.INTERNAL_SERVICE,
            )
            .order_by("-date_opened")
            .values("date_opened")[:1]
        )
        # The enrollment join is multi-valued (a client can have several), so a
        # client appears on several rows -- aggregate per client below.
        rows = self.get_queryset().annotate(
            _case_opened=Subquery(latest_case_opened)
        ).values_list(
            "client_id", "household_membership__household_id",
            "first_name", "last_name", "created_at",
            "enrollments__requested_at", "enrollments__opened_at",
            "enrollments__verified_at", "_case_opened",
        )

        def _max_dt(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return a if a >= b else b

        clients = {}  # cid -> {hid, name, created, requested, completed, case_created}
        for cid, hid, fn, ln, created, req, opened, verified, case_opened in rows:
            requested = req or opened
            c = clients.get(cid)
            if c is None:
                clients[cid] = {
                    "hid": hid,
                    "name": f"{(fn or '').strip()} {(ln or '').strip()}".strip(),
                    "created": created,
                    "requested": requested,
                    "completed": verified,
                    # Scalar per client (subquery), identical on every row.
                    "case_created": case_opened,
                }
            else:
                c["requested"] = _max_dt(c["requested"], requested)
                c["completed"] = _max_dt(c["completed"], verified)

        hh_ids, seen_hh, individuals = [], set(), []
        # hid -> {created, requested, completed, case_created} aggregated across members
        hh_ts = {}
        for cid, c in clients.items():
            hid = c["hid"]
            if hid:
                if hid not in seen_hh:
                    seen_hh.add(hid)
                    hh_ids.append(hid)
                agg = hh_ts.setdefault(
                    hid,
                    {"created": None, "requested": None, "completed": None,
                     "case_created": None},
                )
                for k in ("created", "requested", "completed", "case_created"):
                    agg[k] = _max_dt(agg[k], c[k])
            else:
                individuals.append((cid, c["name"], c))

        # Household sort name = household name, else its primary's name (one query).
        hh_names = {}
        if hh_ids:
            for hid, hname, fn, ln in HouseholdMember.objects.filter(
                household_id__in=hh_ids, is_primary=True
            ).values_list(
                "household_id", "household__name",
                "client__first_name", "client__last_name",
            ):
                hh_names[hid] = (
                    hname or f"{(fn or '').strip()} {(ln or '').strip()}".strip()
                )

        field = sort_field if sort_field in ("created", "requested", "completed", "case_created") else "created"
        entries = [
            {"type": "household", "id": hid, "name": hh_names.get(hid, ""),
             "sort_ts": hh_ts[hid][field]}
            for hid in hh_ids
        ] + [
            {"type": "individual", "id": cid, "name": name, "sort_ts": c[field]}
            for cid, name, c in individuals
        ]

        # Chosen timestamp first (nulls always last), then name (case-insensitive)
        # for stable pagination.
        def _sort_key(e):
            ts = e["sort_ts"]
            name = (e["name"] or "").lower()
            if ts is None:
                return (1, 0.0, name)
            return (0, -ts.timestamp() if descending else ts.timestamp(), name)

        entries.sort(key=_sort_key)
        return entries

    def _logistics_kitchens(self):
        """Active-and-inactive kitchens with their offered menus + restrictions
        prefetched, loaded once per request for the serviceability checks
        (serving_kitchens_for_member filters to ACTIVE itself)."""
        return list(
            Kitchen.objects.all().prefetch_related(
                "kitchen_menu_types__menu_type",
                "kitchen_menu_types__restrictions",
            )
        )

    def _logistics_checks(self, primary_client, member_clients, kitchens, *, is_boxes):
        """Compute the Logistics readiness checkers for one household/individual:
        per-member menu type / allergies / predicted Out-of-Orbit, plus the
        household-level delivery address, requested cadence (delivery weekdays),
        and whether ANY single kitchen can serve every eligible member.

        Returns ``(per_member: {client_id_str: {...}}, aggregate: {...})``."""
        enr = s.active_enrollment(primary_client)
        profiles = {}
        if enr is not None:
            for mp in enr.member_profiles.all():
                if mp.client_id:
                    profiles[mp.client_id] = mp
        required = required_product_for_program(enr.program_name) if enr else None

        per_member, serving_sets = {}, []
        missing_menu = predicted_out = 0
        for c in member_clients:
            mp = profiles.get(c.client_id)
            menu_type = (mp.menu_type if mp else "") or ""
            allergies = _allergy_labels(mp.food_allergies if mp else [])
            if not menu_type:
                # No menu type is its OWN blocker (missing menu) -- it must NOT
                # ALSO be counted as "may get out of orbit" (that double-reported
                # the same gap).
                missing_menu += 1
                per_member[str(c.client_id)] = {
                    "menu_type": "", "allergies": allergies,
                    "predicted_out_of_orbit": False, "predicted_reason": "",
                }
                continue
            # KITCHEN-AWARE out-of-orbit prediction: the member is only a blocker
            # when NO available kitchen can actually serve their menu + allergies.
            # The old kitchen-AGNOSTIC rule (predict_member_out_of_orbit) flagged
            # members a real kitchen could serve, so it hugely over-inflated
            # "Has blockers" and hid them from the kitchen serviceability check.
            serving = {
                sk["kitchen"].pk
                for sk in serving_kitchens_for_member(
                    mp, kitchens=kitchens, required_product=required,
                )
            }
            if not serving:
                predicted_out += 1
                per_member[str(c.client_id)] = {
                    "menu_type": menu_type, "allergies": allergies,
                    "predicted_out_of_orbit": True,
                    "predicted_reason": "No available kitchen can serve this menu + allergies",
                }
            else:
                # A household is servable only if ONE kitchen serves EVERY member
                # (set intersection below).
                serving_sets.append(serving)
                per_member[str(c.client_id)] = {
                    "menu_type": menu_type, "allergies": allergies,
                    "predicted_out_of_orbit": False, "predicted_reason": "",
                }

        kitchen_available = bool(set.intersection(*serving_sets)) if serving_sets else False
        address = _format_address(enr.delivery_address) if enr else ""
        # Split the address into two lines so the Logistics column can render
        # street+apt on one row and city/state/zip on the next (narrower column).
        addr_obj = enr.delivery_address if enr else None
        if addr_obj is not None:
            address_line1 = ", ".join(p for p in [addr_obj.street, addr_obj.unit] if p)
            _region = " ".join(p for p in [addr_obj.state, addr_obj.zip] if p)
            address_line2 = ", ".join(p for p in [addr_obj.city, _region] if p)
        else:
            address_line1 = address_line2 = ""
        weekdays = list(enr.delivery_weekdays or []) if enr else []

        # NB: delivery cadence (weekdays) is CHOSEN in the kitchen-assignment
        # modal, so it is normally unset here -- it's shown as informational
        # ("requested days", if any) and never counts as a readiness blocker.
        blockers = []
        if not address:
            blockers.append("No delivery address")
        if missing_menu:
            blockers.append(f"{missing_menu} missing menu type")
        if predicted_out:
            blockers.append(f"{predicted_out} may get out of orbit")
        if not kitchen_available:
            blockers.append("Kitchen needs review")

        aggregate = {
            "delivery_address": address,
            "delivery_address_line1": address_line1,
            "delivery_address_line2": address_line2,
            "delivery_weekdays": weekdays,
            "is_boxes": is_boxes,
            "kitchen_available": kitchen_available,
            "menu_type_missing": missing_menu,
            "predicted_out_of_orbit": predicted_out,
            "ready": not blockers,
            "blockers": blockers,
        }
        return per_member, aggregate

    def _renderable_keys(self, entries):
        """Set of ``(type, id)`` group keys that will actually RENDER on the
        Logistics page -- i.e. have at least one member not hidden from the
        kitchen-assignment queue (see ``_hidden_in_logistics``). Mirrors the
        member-drop in ``_build_groups_for_page`` but skips the expensive kitchen
        serviceability calc, so it can run over EVERY entry before pagination to
        keep count / total_pages / results consistent (otherwise hidden
        households are counted but dropped at serialization, e.g. "4 of 674")."""
        hh_ids = [e["id"] for e in entries if e["type"] == "household"]
        ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
        keys = set()
        if hh_ids:
            members = (
                HouseholdMember.objects.filter(household_id__in=hh_ids)
                .select_related("client")
                .prefetch_related(
                    "client__enrollments", "client__member_profiles", "client__cases"
                )
            )
            by_hh = {}
            for hm in members:
                by_hh.setdefault(hm.household_id, []).append(hm)
            for hid in hh_ids:
                if any(
                    h.client and not self._hidden_in_logistics(h.client)
                    for h in by_hh.get(hid, [])
                ):
                    keys.add(("household", hid))
        if ind_ids:
            clients = Client.objects.filter(client_id__in=ind_ids).prefetch_related(
                "enrollments", "member_profiles", "cases"
            )
            for c in clients:
                if not self._hidden_in_logistics(c):
                    keys.add(("individual", c.client_id))
        return keys

    def _compute_logistics_checks(self, entries):
        """Compute logistics checkers for EVERY entry (used by the readiness
        filter, which must decide before pagination). Returns
        ``{(type, id): (per_member, aggregate)}``."""
        kitchens = self._logistics_kitchens()
        out = {}
        hh_ids = [e["id"] for e in entries if e["type"] == "household"]
        ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
        if hh_ids:
            members = (
                HouseholdMember.objects.filter(household_id__in=hh_ids)
                .select_related("client")
                .prefetch_related("client__enrollments", "client__member_profiles",
                                  "client__cases")
            )
            by_hh = {}
            for hm in members:
                by_hh.setdefault(hm.household_id, []).append(hm)
            for hid in hh_ids:
                hms = [
                    h for h in by_hh.get(hid, [])
                    if h.client and not self._hidden_in_logistics(h.client)
                ]
                if not hms:
                    continue
                primary_hm = next((h for h in hms if h.is_primary), hms[0])
                is_boxes = self._service_type_for_client(primary_hm.client) == "boxes"
                out[("household", hid)] = self._logistics_checks(
                    primary_hm.client, [h.client for h in hms], kitchens, is_boxes=is_boxes,
                )
        if ind_ids:
            clients = Client.objects.filter(client_id__in=ind_ids).prefetch_related(
                "enrollments", "member_profiles", "cases",
            )
            for c in clients:
                if self._hidden_in_logistics(c):
                    continue
                is_boxes = self._service_type_for_client(c) == "boxes"
                out[("individual", c.client_id)] = self._logistics_checks(
                    c, [c], kitchens, is_boxes=is_boxes,
                )
        return out

    def _attach_logistics(self, group, primary_client, member_clients, member_data,
                          kitchens, *, precomputed=None):
        """Attach the logistics checkers to a group: per-member fields onto each
        member dict and the household aggregate as ``group["logistics"]``. Uses
        ``precomputed`` (from the readiness filter pass) when available, else
        computes for this group."""
        if precomputed is not None:
            per_member, aggregate = precomputed
        else:
            per_member, aggregate = self._logistics_checks(
                primary_client, member_clients, kitchens,
                is_boxes=group.get("service_type") == "boxes",
            )
        for md in member_data:
            md.update(per_member.get(md["id"], {}))
        group["logistics"] = aggregate

    def _build_groups_for_page(self, entries, checks=None):
        """Serialize ONLY the groups on the current page, preserving order."""
        hh_ids = [e["id"] for e in entries if e["type"] == "household"]
        ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
        groups_by_key = {}
        # Logistics (kitchen-assignment) hides out-of-orbit / finished-case
        # members from each household roster (see _hidden_in_logistics).
        logistics = (self.request.query_params.get("scope") or "").strip() == "logistics"
        kitchens = self._logistics_kitchens() if logistics else None

        if hh_ids:
            members = (
                HouseholdMember.objects.filter(household_id__in=hh_ids)
                .select_related("household", "client")
                .prefetch_related(
                    "client__insurances", "client__military_profile",
                    Prefetch(
                        "client__enrollments",
                        queryset=EnrollmentVerification.objects.select_related(
                            "requested_by", "verified_by"
                        ),
                    ),
                    "client__member_profiles", "client__cases",
                    # Household roster + household-level enrollments for the
                    # serializer's household_primary_id and active_enrollment
                    # dependent fallback -- prefetch so they cost a few queries
                    # per page instead of one-or-more per member row (N+1).
                    "client__household_membership__household__members",
                    "client__household_membership__household__enrollment_verifications",
                )
                .order_by("-is_primary", "added_at")
            )
            by_hh = {}
            for hm in members:
                by_hh.setdefault(hm.household_id, []).append(hm)
            for hid in hh_ids:
                hms = by_hh.get(hid)
                if not hms:
                    continue
                if logistics:
                    hms = [
                        h for h in hms
                        if h.client and not self._hidden_in_logistics(h.client)
                    ]
                    if not hms:
                        continue  # whole household hidden -> drop from the page
                primary_hm = next((h for h in hms if h.is_primary), hms[0])
                member_data = [
                    self._serialize_member(h.client, h.is_primary, h.relationship)
                    for h in hms
                ]
                primary_data = next(
                    (m for m in member_data if m["id"] == str(primary_hm.client_id)),
                    member_data[0],
                )
                group = {
                    "id": str(hid),
                    "type": "household",
                    "name": primary_hm.household.name or primary_data["name"],
                    "member_count": len(member_data),
                    "service_type": self._service_type_for_client(primary_hm.client),
                    "primary": primary_data,
                    "members": member_data,
                }
                if logistics:
                    self._attach_logistics(
                        group, primary_hm.client, [h.client for h in hms],
                        member_data, kitchens,
                        precomputed=(checks or {}).get(("household", hid)),
                    )
                groups_by_key[("household", hid)] = group

        if ind_ids:
            clients = Client.objects.filter(client_id__in=ind_ids).prefetch_related(
                *MEMBER_LIST_PREFETCH
            )
            for c in clients:
                if logistics and self._hidden_in_logistics(c):
                    continue  # out-of-orbit / finished-case individual
                primary_data = self._serialize_member(c, True)
                group = {
                    "id": str(c.client_id),
                    "type": "individual",
                    "name": primary_data["name"],
                    "member_count": 1,
                    "service_type": self._service_type_for_client(c),
                    "primary": primary_data,
                    "members": [primary_data],
                }
                if logistics:
                    self._attach_logistics(
                        group, c, [c], [primary_data], kitchens,
                        precomputed=(checks or {}).get(("individual", c.client_id)),
                    )
                groups_by_key[("individual", c.client_id)] = group

        # Preserve the paginated order from `entries`.
        return [
            groups_by_key[(e["type"], e["id"])]
            for e in entries
            if (e["type"], e["id"]) in groups_by_key
        ]

    def get(self, request):
        # Flat mode: one row per individual member (no household grouping),
        # used by the Members page. Otherwise return household groups.
        if request.query_params.get("flat"):
            # Order + paginate in SQL (LIMIT/OFFSET) so we only ever serialize
            # one page; serializing/sorting the whole clients table per request
            # does not scale once the full member base is imported. Sortable by
            # the Created / Last Updated columns (``sort`` + ``dir``); default is
            # most-recently-created first. Rows with a null date sort last; name
            # (case-insensitive "First Last") breaks ties.
            #
            # The Created column now sorts by the member's most-recently-opened
            # INTERNAL-SERVICE case date (matching the O: rows it renders), NOT
            # the Client record's own ``created_at``. A correlated subquery pulls
            # that one date per client so ordering over the multi-valued cases
            # relation can't multiply rows. ``updated`` still sorts on the member
            # ``updated_at``. Rows with a null sort key sort last; name breaks ties.
            sort_key = (request.query_params.get("sort") or "created").strip().lower()
            descending = (request.query_params.get("dir") or "desc").strip().lower() != "asc"
            qs = self.get_queryset()
            if sort_key == "updated":
                col = F("updated_at")
            else:
                latest_case_opened = (
                    Case.objects.filter(
                        client=OuterRef("pk"),
                        case_type=CaseType.INTERNAL_SERVICE,
                    )
                    .order_by("-date_opened")
                    .values("date_opened")[:1]
                )
                qs = qs.annotate(_case_opened=Subquery(latest_case_opened))
                col = F("_case_opened")
            primary = col.desc(nulls_last=True) if descending else col.asc(nulls_last=True)
            qs = qs.order_by(
                primary,
                Lower("first_name"),
                Lower("last_name"),
            )
            page = self.paginate_queryset(qs)
            data = []
            for c in page:
                row = s.MemberListSerializer(c).data
                # Meals/Boxes kind (household-wide) for the row's service label.
                row["service_type"] = self._service_type_for_client(c)
                data.append(row)
            # Stamp how each member was first created (Extension / Import / …),
            # shown under the lead source in the Source column. Batched over the
            # page's client ids to avoid an N+1.
            via = self._added_via_map([r["id"] for r in data])
            for row in data:
                src, label = via.get(row["id"], ("", "Unknown"))
                row["added_via"] = src
                row["added_via_label"] = label
            # Stamp the CareCircle team of each member's internal-service case
            # creator, shown next to the creator name in the Created column.
            teams = self._case_team_map(page)
            for row in data:
                row["case_created_by_team"] = teams.get(row["id"], "")
            return self.get_paginated_response(data)
        # Grouped mode (Verification / Logistics): build the ordered group keys
        # cheaply, paginate THEM, and serialize only the current page's groups
        # (previously the whole scoped set was serialized on every request).
        # Sortable by the Verification page Requested / Completed columns and the
        # Urgent Care "Case Created" column (``sort`` + ``dir``); default is
        # most-recently-created first.
        group_sort = {
            "requested": "requested", "completed": "completed",
            "case_created": "case_created",
        }.get((request.query_params.get("sort") or "").strip().lower(), "created")
        group_desc = (request.query_params.get("dir") or "desc").strip().lower() != "asc"
        entries = self._group_entries(group_sort, group_desc)
        scope = (request.query_params.get("scope") or "").strip()
        checks = None
        if scope == "logistics":
            # Every logistics request must drop groups that would render EMPTY
            # (all members hidden from the kitchen-assignment queue) BEFORE
            # pagination, so count / total_pages / results agree. Without this the
            # hidden groups inflate the count but vanish at serialization, so a
            # page shows only a handful of rows (e.g. "Showing 4 of 674").
            readiness = (request.query_params.get("readiness") or "").strip().lower()
            if readiness in ("ready", "blockers"):
                # Readiness split needs the full checks (menu / address / kitchen
                # serviceability), reused when serializing the page. Hidden groups
                # have NO checks entry, so they're excluded from both ready and
                # blockers here.
                checks = self._compute_logistics_checks(entries)
                want_ready = readiness == "ready"
                entries = [
                    e for e in entries
                    if (e["type"], e["id"]) in checks
                    and checks[(e["type"], e["id"])][1]["ready"] == want_ready
                ]
            else:
                # No readiness filter: cheap hidden-only pass (skips the kitchen
                # calc); the page's checks are computed per-page below.
                renderable = self._renderable_keys(entries)
                entries = [e for e in entries if (e["type"], e["id"]) in renderable]
        page = self.paginate_queryset(entries)
        groups = self._build_groups_for_page(page or [], checks=checks)
        # Stamp the Source (extension vs import) + case-creator Team badges onto
        # each member. Needed by the Urgent Care ("Need Attention") triage list
        # AND by the household-grouped Members page (no scope), whose rows render
        # those same columns. The Verification page also shows a Team column, so
        # it gets the team badge (but not the Source/added-via stamp). Logistics
        # needs neither.
        if scope in ("need_attention", ""):
            self._stamp_added_via(groups)
            self._stamp_case_teams(groups)
        elif scope == "verification":
            self._stamp_case_teams(groups)
        return self.get_paginated_response(groups)


class UnlinkedMembersListView(PortalGenericAPIView):
    """Urgent Care -> Un-Linked Members tab.

    Members who hold an eligibility/navigation case but NO internal-service case
    and are NOT attached to any household -- the "unaffiliated" population the
    dashboard's Total Members card surfaces. Optionally scoped by the
    eligibility/navigation case's opened date (``case_from`` / ``case_to``).

    For each member we also probe whether their Medicaid ID or member id (client
    UUID) is referenced in ANY OTHER member's case description, so an agent can
    jump to the case that actually "owns" them (and open it in Unite Us / CRM).
    """

    @staticmethod
    def base_queryset(case_from=None, case_to=None):
        """The un-linked/"unaffiliated" population: members with an
        eligibility/navigation case (optionally opened within [case_from,
        case_to]) but NO internal-service case, who are NOT attached to any
        household. Shared with the executive dashboard's Total Members card so
        the "Unaffiliated" number matches this tab exactly."""
        en_case = Case.objects.filter(
            client=OuterRef("pk"),
            case_type__in=(CaseType.ELIGIBILITY, CaseType.NAVIGATION),
        )
        if case_from:
            en_case = en_case.filter(date_opened__date__gte=case_from)
        if case_to:
            en_case = en_case.filter(date_opened__date__lte=case_to)
        internal_case = Case.objects.filter(
            client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
        )
        return (
            Client.objects.filter(household_membership__isnull=True)
            .annotate(_has_en=Exists(en_case), _has_ic=Exists(internal_case))
            .filter(_has_en=True, _has_ic=False)
        )

    def get_queryset(self):
        params = self.request.query_params
        case_from = _parse_date(params.get("case_from"))
        case_to = _parse_date(params.get("case_to"))

        qs = self.base_queryset(case_from, case_to).prefetch_related(
            "insurances", "cases", "assessments"
        )

        search = (params.get("search") or "").strip()
        if search:
            cond = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(insurances__external_member_id__icontains=search)
            )
            parts = search.split()
            if len(parts) >= 2:
                cond |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
            try:
                cond |= Q(client_id=uuid.UUID(search))
            except (ValueError, TypeError, AttributeError):
                pass
            qs = qs.filter(cond)

        # Optionally keep only members that ARE referenced in some other member's
        # case description (the "Referenced In" column). Resolved in a single
        # pass over all descriptions (see _referenced_client_ids) rather than a
        # per-candidate icontains EXISTS, which scanned every case per member and
        # timed out on the full population.
        if (params.get("referenced") or "").strip().lower() in ("1", "true", "yes"):
            qs = qs.filter(pk__in=self._referenced_client_ids(qs))

        return qs.order_by(Lower("first_name"), Lower("last_name")).distinct()

    # Client UUID + generic alphanumeric-id tokens (>=6 chars, e.g. Medicaid
    # member ids) as they'd appear inside a free-text case description.
    _UUID_RE = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )
    _TOKEN_RE = re.compile(r"[a-z0-9]{6,}")

    @classmethod
    def _referenced_client_ids(cls, base_qs):
        """PKs of the candidate members whose client UUID or Medicaid member id
        appears in ANY case description. Builds the set of id-like tokens found
        across every non-empty description once, then keeps only candidates whose
        UUID / Medicaid id is among them -- O(descriptions + candidates) instead
        of a correlated icontains scan per candidate."""
        tokens = set()
        for desc in (
            Case.objects.exclude(case_description="")
            .values_list("case_description", flat=True)
            .iterator(chunk_size=2000)
        ):
            low = desc.lower()
            tokens.update(cls._UUID_RE.findall(low))
            tokens.update(cls._TOKEN_RE.findall(low))

        # candidate client_id -> Medicaid member id (lowercased)
        medicaid = {
            r["client_id"]: (r["insurances__external_member_id"] or "").lower()
            for r in base_qs.filter(
                insurances__plan_type=InsurancePlanType.MEDICAID
            )
            .exclude(insurances__external_member_id="")
            .values("client_id", "insurances__external_member_id")
        }

        keep = []
        for cid in base_qs.values_list("client_id", flat=True):
            m = medicaid.get(cid)
            if str(cid).lower() in tokens or (m and m in tokens):
                keep.append(cid)
        return keep

    @staticmethod
    def _case_of_type(client, case_type):
        """The client's most-recent case of ``case_type`` (for the per-type
        created date + creator columns). None when the client has none."""
        dated = [
            c
            for c in client.cases.all()
            if c.case_type == case_type and c.date_opened
        ]
        return max(dated, key=lambda c: c.date_opened) if dated else None

    @staticmethod
    def _case_cell(case):
        """Serialize a case's created date + creator for a table column."""
        if case is None:
            return {"created_at": None, "created_by": ""}
        return {
            "created_at": case.date_opened.isoformat() if case.date_opened else None,
            "created_by": case.created_by_name or case.primary_worker_name or "",
        }

    @staticmethod
    def _eligible_services(client):
        """Unique eligible-service names across ALL of the client's eligibility
        assessments (``Assessment.eligible_services``), order preserved. Items
        are usually plain strings, but tolerate ``{"name"/"code": ...}`` dicts."""
        seen = []
        for a in client.assessments.all():
            for raw in a.eligible_services or []:
                name = (
                    (raw.get("name") or raw.get("code"))
                    if isinstance(raw, dict)
                    else raw
                )
                if isinstance(name, str) and name.strip() and name not in seen:
                    seen.append(name.strip())
        return seen

    @staticmethod
    def _description_match(client):
        """If this member's member id (client UUID) or ANY of their Medicaid IDs
        appears in ANOTHER member's case description, return that owning case's
        info so the agent can open it. None when there's no reference.

        A member can carry more than one Medicaid insurance row, so we probe
        EVERY Medicaid id -- not just the single canonical one -- to stay in
        step with the referenced-in filter (``_referenced_client_ids``), which
        also scans all of them. Probing only the canonical id left members that
        were referenced by a secondary Medicaid id showing an empty column."""
        cid = str(client.client_id)
        medicaid_ids = [
            mid
            for i in client.insurances.all()
            if i.plan_type == InsurancePlanType.MEDICAID
            and (mid := (i.external_member_id or "").strip())
        ]
        cond = Q(case_description__icontains=cid)
        for mid in medicaid_ids:
            cond |= Q(case_description__icontains=mid)
        match = (
            Case.objects.filter(cond)
            .exclude(client_id=client.client_id)
            .exclude(case_description="")
            .select_related("client")
            .order_by("-date_opened")
            .first()
        )
        if match is None:
            return None
        desc = (match.case_description or "").lower()
        matched_on = (
            "medicaid"
            if any(mid.lower() in desc for mid in medicaid_ids)
            else "member_id"
        )
        return {
            "owner_id": str(match.client_id),
            "owner_name": s._full_name(match.client) if match.client else "",
            "case_id": str(match.case_id),
            "program_name": match.program_name or match.service_type or "",
            "matched_on": matched_on,
        }

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        rows = []
        for c in page or []:
            rows.append(
                {
                    "id": str(c.client_id),
                    "name": s._full_name(c),
                    "date_of_birth": c.date_of_birth.isoformat()
                    if c.date_of_birth
                    else None,
                    "medicaid_id": s.medicaid_member_id(c) or "",
                    "eligible_services": self._eligible_services(c),
                    # Eligibility is checked first, then navigation -- surfaced as
                    # two separate columns so a member with only one is obvious.
                    "eligibility_case": self._case_cell(
                        self._case_of_type(c, CaseType.ELIGIBILITY)
                    ),
                    "navigation_case": self._case_cell(
                        self._case_of_type(c, CaseType.NAVIGATION)
                    ),
                    "description_match": self._description_match(c),
                }
            )
        return self.get_paginated_response(rows)


class NoNavigationMembersListView(UnlinkedMembersListView):
    """Urgent Care -> No Navigation tab.

    Members who HOLD an internal-service case but have NO navigation case at all
    (regardless of the navigation case's status). Reuses the row
    serialization + eligibility/navigation column helpers from
    :class:`UnlinkedMembersListView`; only the population differs. Optionally
    scoped by the internal-service case's opened date (``case_from`` /
    ``case_to``).
    """

    def get_queryset(self):
        params = self.request.query_params
        case_from = _parse_date(params.get("case_from"))
        case_to = _parse_date(params.get("case_to"))

        # Internal-service case, optionally opened within [from, to].
        internal_case = Case.objects.filter(
            client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
        )
        if case_from:
            internal_case = internal_case.filter(date_opened__date__gte=case_from)
        if case_to:
            internal_case = internal_case.filter(date_opened__date__lte=case_to)
        # ANY navigation case (regardless of status) disqualifies the member --
        # this tab is for members who have NO navigation case at all.
        nav_case = Case.objects.filter(
            client=OuterRef("pk"), case_type=CaseType.NAVIGATION,
        )

        qs = (
            Client.objects.annotate(
                _has_ic=Exists(internal_case),
                _has_nav=Exists(nav_case),
            )
            .filter(_has_ic=True, _has_nav=False)
            .prefetch_related("insurances", "cases", "assessments")
        )

        search = (params.get("search") or "").strip()
        if search:
            cond = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(insurances__external_member_id__icontains=search)
            )
            parts = search.split()
            if len(parts) >= 2:
                cond |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
            try:
                cond |= Q(client_id=uuid.UUID(search))
            except (ValueError, TypeError, AttributeError):
                pass
            qs = qs.filter(cond)

        return qs.order_by(Lower("first_name"), Lower("last_name")).distinct()


class NeedAttestationMembersListView(UnlinkedMembersListView):
    """Urgent Care -> Need Attestation tab.

    Members the ext flagged as needing a provider (doctor) attestation:
    ``Client.attestation_needed=True``. On the ext this is set when an
    eligibility Assessment's ``"<Population> - Verification Method"`` answer is
    ``"Provider Attestation"`` (replayed onto our data by the
    ``backfill_attestation_needed`` command). Reuses the row serialization +
    eligibility/navigation column helpers from :class:`UnlinkedMembersListView`;
    only the population differs.
    """

    # The eligibility-assessment answer that means "provider/doctor attestation
    # required" (mirrors backfill_attestation_needed).
    _PROVIDER_ATTESTATION = "provider attestation"
    _VERIFICATION_METHOD_SUFFIX = "verification method"

    def get_queryset(self):
        params = self.request.query_params

        qs = Client.objects.filter(attestation_needed=True).prefetch_related(
            "insurances", "cases", "assessments"
        )

        search = (params.get("search") or "").strip()
        if search:
            cond = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(insurances__external_member_id__icontains=search)
            )
            parts = search.split()
            if len(parts) >= 2:
                cond |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
            try:
                cond |= Q(client_id=uuid.UUID(search))
            except (ValueError, TypeError, AttributeError):
                pass
            qs = qs.filter(cond)

        return qs.order_by(Lower("first_name"), Lower("last_name")).distinct()

    @classmethod
    def _attestation_qa(cls, client):
        """The eligibility-assessment question + answer that flags this member as
        needing provider attestation (a ``"… - Verification Method"`` answered
        ``"Provider Attestation"``). None when not found on any assessment."""
        for a in client.assessments.all():
            for qa in a.questions_answers or []:
                question = (qa.get("question") or "").strip()
                answer = (qa.get("answer") or "").strip()
                if question.lower().endswith(cls._VERIFICATION_METHOD_SUFFIX) and (
                    answer.lower() == cls._PROVIDER_ATTESTATION
                ):
                    return {
                        "question": question,
                        "answer": answer,
                        "form_name": a.form_name or "",
                    }
        return None

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        rows = []
        for c in page or []:
            rows.append(
                {
                    "id": str(c.client_id),
                    "name": s._full_name(c),
                    "date_of_birth": c.date_of_birth.isoformat()
                    if c.date_of_birth
                    else None,
                    "medicaid_id": s.medicaid_member_id(c) or "",
                    "eligibility_case": self._case_cell(
                        self._case_of_type(c, CaseType.ELIGIBILITY)
                    ),
                    "navigation_case": self._case_cell(
                        self._case_of_type(c, CaseType.NAVIGATION)
                    ),
                    # The screening question + answer we keyed the flag on.
                    "attestation": self._attestation_qa(c),
                }
            )
        return self.get_paginated_response(rows)


class MembersStatsView(PortalAPIView):
    def get(self, request):
        qs = Client.objects.all()
        scope = (request.query_params.get("scope") or "").strip()
        # Mirror the Verification list's scope so the chip counts match the rows
        # actually shown: the full verification history (pending + ever-verified)
        # plus the internal-service-primary eligibility filter. The joins make
        # rows non-unique, so all counts below must be DISTINCT on the client.
        if scope == "verification":
            qs = require_internal_service_primary(
                qs.filter(verification_scope_q())
            ).distinct()
        else:
            scope_stages = SCOPE_TO_STAGES.get(scope)
            if scope_stages:
                qs = qs.filter(lifecycle_stage__in=scope_stages)
        # Date-period filter (mirrors the list) so the chip counts match the
        # rows shown for the selected window.
        period = request.query_params.get("period")
        qs = apply_period_filter(qs, period)
        # Requested/completed date-range filters (mirror the list).
        qs, date_filtered = apply_verification_date_filters(qs, request.query_params)
        if period_date_range(period) or date_filtered:
            qs = qs.distinct()
        counts = {"total": qs.count()}
        for label, stages in STATUS_TO_STAGES.items():
            counts[label.lower()] = qs.filter(lifecycle_stage__in=stages).count()
        # Verification work-area chips are split on whether the verification was
        # actually COMPLETED (enrollment stage), not lifecycle_stage -- so a
        # case-auth-driven waiting_authorization counts as Pending, not Verified.
        # Override the lifecycle-based counts above to match the list filters.
        if scope == "verification":
            completed_q = verification_completed_q()
            counts["verified_awaiting"] = qs.filter(completed_q).distinct().count()
            counts["pending_verification"] = qs.exclude(completed_q).distinct().count()
        # Authorization counts (separate dimension): how many members' GOVERNING
        # internal-service case is in each authorization status. Powers the
        # Authorization filter chips; uses the same governing-aware filter as the
        # list so counts match the rows.
        counts["authorization"] = {
            key: apply_authorization_filter(qs, key).distinct().count()
            for key in AUTHORIZATION_FILTERS
        }
        # Raw per-stage counts (powers stage-specific filter chips on the
        # Verification page).
        counts["stages"] = {
            row["lifecycle_stage"]: row["n"]
            for row in qs.values("lifecycle_stage").annotate(
                n=Count("id", distinct=True)
            )
        }
        return Response(counts)


def _get_member(client_id):
    return get_object_or_404(
        Client.objects.prefetch_related(
            "insurances", "military_profile", "addresses", "tickets__type",
            "enrollments", "cases",
        ),
        pk=client_id,
    )


class MemberDetailView(PortalAPIView):
    def get(self, request, client_id):
        client = _get_member(client_id)
        return Response(s.MemberDetailSerializer(client).data)


class MemberInsuranceView(PortalAPIView):
    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        plans = client.insurances.all()
        return Response(s.PortalInsuranceSerializer(plans, many=True).data)


class MemberSocialCoverageView(PortalAPIView):
    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        plans = client.social_care_coverages.all()
        return Response(s.PortalSocialCoverageSerializer(plans, many=True).data)


class MemberDoctorView(PortalAPIView):
    """GET/PATCH /members/<client_id>/doctor/ — the member's Doctor/PCP
    (attestation) information shown and edited on the profile's Attestation tab.

    The doctor fields are collected by the ext when a member needs a provider
    attestation (``attestation_needed=True``); this lets an agent view, enter or
    correct them directly. PATCH accepts any subset of the doctor fields.
    """

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        return Response(s.PortalDoctorSerializer(client).data)

    def patch(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        ser = s.PortalDoctorSerializer(client, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)


class MemberPhonesView(PortalAPIView):
    """GET/POST /members/<client_id>/phones/ — list and add the client's phone
    numbers (the Communication Preferences card on the member profile). Shares
    the ClientPhone model + response shape with the extension caller-ID flow."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        phones = ClientPhone.objects.filter(client_id=client_id)
        return Response([_phone_dict(p) for p in phones])

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        number = (request.data.get("number") or "").strip()
        normalized = ClientPhone.normalize(number)
        if not normalized:
            return Response(
                {"error": "A valid phone number (at least 10 digits) is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        label = (request.data.get("label") or "").strip()
        # Idempotent on (client, normalized): re-adding the same number refreshes
        # it rather than tripping the unique constraint.
        phone, created = ClientPhone.objects.get_or_create(
            client=client,
            normalized=normalized,
            defaults={"raw": number, "label": label, "source": ClientPhoneSource.AGENT},
        )
        phone.last_seen_at = timezone.now()
        if not created and label and not phone.label:
            phone.label = label
        phone.save(update_fields=["last_seen_at", "label"])
        # First number a client ever gets becomes primary by default.
        make_primary = bool(request.data.get("is_primary")) or (
            created and not ClientPhone.objects.filter(
                client=client, is_primary=True
            ).exclude(pk=phone.pk).exists()
        )
        if make_primary:
            ClientPhone.objects.filter(client=client, is_primary=True).exclude(
                pk=phone.pk
            ).update(is_primary=False)
            phone.is_primary = True
            phone.save(update_fields=["is_primary"])
        return Response(
            _phone_dict(phone),
            status=http.HTTP_201_CREATED if created else http.HTTP_200_OK,
        )


class MemberPhoneDetailView(PortalAPIView):
    """PATCH/DELETE /members/<client_id>/phones/<client_phone_id>/ — edit the
    label / primary flag, or remove a number."""

    def patch(self, request, client_id, client_phone_id):
        phone = get_object_or_404(
            ClientPhone, pk=client_phone_id, client_id=client_id
        )
        if bool(request.data.get("is_primary")):
            ClientPhone.objects.filter(
                client_id=client_id, is_primary=True
            ).exclude(pk=phone.pk).update(is_primary=False)
            phone.is_primary = True
            phone.save(update_fields=["is_primary"])
        if "label" in request.data:
            phone.label = (request.data.get("label") or "").strip()
            phone.save(update_fields=["label"])
        return Response(_phone_dict(phone))

    def delete(self, request, client_id, client_phone_id):
        phone = get_object_or_404(
            ClientPhone, pk=client_phone_id, client_id=client_id
        )
        phone.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class MemberHistoryView(PortalGenericAPIView):
    serializer_class = s.HistoryEventSummarySerializer

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = TimelineEvent.objects.filter(client_id=client_id)
        page = self.paginate_queryset(qs)
        ctx = self.get_serializer_context()
        ctx["actor_names"] = s.build_actor_name_map(page)
        data = self.get_serializer(page, many=True, context=ctx).data
        return self.get_paginated_response(data)


class MemberHistoryDetailView(PortalAPIView):
    def get(self, request, client_id, event_id):
        event = get_object_or_404(
            TimelineEvent, pk=event_id, client_id=client_id
        )
        return Response(s.HistoryEventDetailSerializer(event).data)


class MemberOrdersView(PortalGenericAPIView):
    """Purchase orders that include a delivery for this member."""

    serializer_class = s.PortalMemberOrderSerializer

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = (
            PurchaseOrder.objects.filter(delivery_orders__member_id=client_id)
            .distinct()
            .prefetch_related("delivery_orders", "kitchen", "delivery_company")
        )
        # Optional ?enrollment=<id> scopes orders to a SPECIFIC (superseded)
        # enrollment. DeliveryOrders aren't enrollment-linked, so scope by that
        # enrollment's delivery window (its plan's start/end dates) -- the orders
        # delivered while that enrollment was the active program.
        override = (request.query_params.get("enrollment") or "").strip()
        if override:
            window = MemberDeliverySchedule.objects.filter(
                enrollment_id=override
            ).aggregate(start=Min("starts_on"), end=Max("ends_on"))
            start, end = window.get("start"), window.get("end")
            if start:
                qs = qs.filter(delivery_orders__expected_delivery_date__gte=start)
            if end:
                qs = qs.filter(delivery_orders__expected_delivery_date__lte=end)
            if not start and not end:
                qs = qs.none()
        status_val = (request.query_params.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            qs = qs.filter(status=status_val)
        page = self.paginate_queryset(qs)
        data = self.get_serializer(
            page, many=True, context={"member_id": str(client_id)}
        ).data
        return self.get_paginated_response(data)


class MemberHouseholdView(PortalAPIView):
    """Household tab: address + per-member dietary, from the active enrollment.

    An optional ``?enrollment=<id>`` query param scopes the response to a
    SPECIFIC (e.g. superseded/closed) enrollment of the client instead of the
    active one -- used to show a prior enrollment's tab read-only. The enrollment
    must belong to the client. The response carries ``read_only=True`` in that
    case so the frontend disables every write control.
    """

    def _enrollment(self, client_id):
        client = get_object_or_404(Client, pk=client_id)
        override = None
        req = getattr(self, "request", None)
        if req is not None:
            override = (req.query_params.get("enrollment") or "").strip()
        if override:
            return get_object_or_404(
                EnrollmentVerification, pk=override, client_id=client_id
            )
        return s.active_enrollment(client)

    def _is_scoped_override(self, client_id, enr):
        """True when the response is scoped to a non-active enrollment (read-only)."""
        client = get_object_or_404(Client, pk=client_id)
        active = s.active_enrollment(client)
        return enr is not None and active is not None and enr.pk != active.pk

    def get(self, request, client_id):
        enr = self._enrollment(client_id)
        if enr is None:
            return Response({"enrollment": None, "address": None, "members": []})
        read_only = self._is_scoped_override(client_id, enr)
        # Heal any drift between the household roster and this enrollment's
        # per-member profiles, so members tied via the extension picker (which
        # only writes a HouseholdMember row) appear here with dietary/menu/status
        # and share the enrollment's address/service. Idempotent. Skipped for a
        # read-only scoped (superseded) enrollment -- never mutate history.
        if not read_only:
            sync_household_members(enr.client, enrollment=enr)
        members = enr.member_profiles.select_related(
            "client__household_membership"
        ).all()
        addr = enr.delivery_address
        # Product kind (Meals / Boxes). `kind` is the scope the enrollment was
        # VERIFIED under (product_type_override, stamped at verification /
        # backfill). `detected` is the governing case's kind, DRIVEN BY THE CASE
        # (its program name) -- the immutable baseline. They differ only on a case
        # mismatch, which is the ONLY time the inline switch is offered.
        kind = product_kind_for_enrollment(enr)
        detected = detected_product_kind_for_enrollment(enr)
        mismatch = (
            kind is not None
            and detected is not None
            and kind != detected
        )
        cadence = current_household_cadence(enr)
        cadence_row = Cadence.objects.filter(code=cadence).first() if cadence else None
        # Household vs Individual scope. The enrollment carries the scope it was
        # VERIFIED under (household_type_override, stamped at verification /
        # backfill). The GOVERNING case's household_type is the immutable baseline
        # (driven by the source case). When the two diverge -- e.g. a new
        # Individual governing case replaces the Household one this was verified
        # for -- that's a case mismatch the agent can reconcile from this tab.
        from ..serializers import derive_household_type

        gov_case = governing_internal_case(enr) or enr.case
        # The governing case scope is DRIVEN BY THE CASE -- derived live from its
        # program name, never the stored household_type cache.
        detected_ht = (
            derive_household_type(None, getattr(gov_case, "program_name", ""))
            if gov_case is not None else ""
        )
        override_ht = enr.household_type_override or ""
        effective_ht = override_ht or detected_ht or CaseHouseholdType.INDIVIDUAL
        # Mismatch: the enrollment's verified scope differs from the governing
        # case's scope. Only then is the inline scope switch offered.
        household_type_mismatch = bool(detected_ht) and effective_ht != detected_ht
        from ..services.lifecycle import program_status
        ps = program_status(enr)
        # A CLOSED program (no open internal-service case) is frozen: the whole
        # tab is read-only history. The frontend uses ``program_locked`` to
        # disable every write control, and both can_* flags are forced False so a
        # closure hold can't be resumed and an active-but-orphaned enrollment
        # can't be held.
        program_locked = _program_locked(enr)
        return Response(
            {
                "enrollment": {
                    "id": enr.pk, "code": enr.code, "stage": enr.stage,
                    # Computed per-program status (merges verification stage +
                    # governing case authorization) shown on the accordion row.
                    "program_status": ps.value,
                    "program_status_label": ps.label,
                    # True when the program's governing internal-service case is
                    # closed -- the tab is frozen (read-only history).
                    "program_locked": program_locked,
                    # Per-program On Hold controls (the household-wide hold was
                    # replaced by a per-program hold on the accordion row).
                    "on_hold": enr.stage == EnrollmentStage.ON_HOLD,
                    "can_hold": not program_locked and enr.stage not in (
                        EnrollmentStage.ON_HOLD, EnrollmentStage.CANCELLED,
                        EnrollmentStage.CLOSED, EnrollmentStage.SERVICE_COMPLETE,
                    ),
                    # Resume (On Hold -> prior stage) is offered only while the
                    # program is held AND no member is on the INELIGIBLE off-ramp:
                    # an ineligibility hold is CareCircle-unfixable, so it can be
                    # lifted only by the data recovering (reconcile_client_
                    # eligibility), never by a manual Resume. Also forced off for a
                    # closed program (a closure hold reopens only via a new case).
                    # The frontend hides the Resume button when this is False.
                    "can_resume": (
                        not program_locked
                        and enr.stage == EnrollmentStage.ON_HOLD
                        and not _enrollment_resume_blocked_by_ineligibility(enr)
                    ),
                    "kitchen_id": str(enr.kitchen_id) if enr.kitchen_id else None,
                    "kitchen_name": enr.kitchen.name if enr.kitchen_id else "",
                    # Product kind. `service_type` is the VERIFIED kind; the
                    # `product_type_case_*` is the governing case's (immutable)
                    # kind. They differ only on a case mismatch, the ONLY time the
                    # inline switch is offered (product_type_mismatch=True).
                    "service_type": kind.value if kind else "",
                    "service_type_label": kind.label if kind else "",
                    "product_type_case": detected.value if detected else "",
                    "product_type_case_label": detected.label if detected else "",
                    "product_type_mismatch": mismatch,
                    "product_type_options": [
                        {"value": k.value, "label": k.label} for k in ProductTypeKind
                    ],
                    # Household vs Individual scope. `household_type` is the scope
                    # the enrollment was VERIFIED under; `household_type_case` is
                    # the governing case's (immutable) scope. They differ only on a
                    # case mismatch, which is the ONLY time the inline switch is
                    # offered (household_type_mismatch=True).
                    "household_type": effective_ht,
                    "household_type_label": CaseHouseholdType(effective_ht).label,
                    "household_type_case": detected_ht or "",
                    "household_type_case_label": (
                        CaseHouseholdType(detected_ht).label if detected_ht else ""
                    ),
                    "household_type_mismatch": household_type_mismatch,
                    "household_type_options": [
                        {"value": h.value, "label": h.label} for h in CaseHouseholdType
                    ],
                    "cadence": cadence,
                    "cadence_label": (cadence_row.label if cadence_row else "") or cadence,
                    "cadence_options": cadence_options_for_kind(kind),
                    "supersedes": {
                        "id": enr.supersedes_id,
                        "case_id": str(enr.supersedes.case.case_id) if enr.supersedes and enr.supersedes.case else None,
                        "program_name": enr.supersedes.program_name or "",
                        "stage": enr.supersedes.stage if enr.supersedes else None,
                    } if enr.supersedes else None,
                    # Full supersession chain (this enrollment's older, replaced
                    # enrollments from prior cases), newest first. Each is shown as
                    # a read-only accordion on the Program tab with its own history.
                    # Only the ACTIVE (non-scoped) response carries the chain -- a
                    # scoped prior enrollment shows just its own tab.
                    "prior_enrollments": [] if read_only else _prior_enrollment_chain(enr),
                    # This enrollment's OWN case description (the new case on the
                    # active program; the old case on a superseded one).
                    "case_description": (enr.case.case_description or "") if enr.case else "",
                    "case_id": str(enr.case.case_id) if enr.case else None,
                },
                # True when scoped to a superseded enrollment: the frontend hides
                # every edit/action control and renders the tab read-only.
                "read_only": read_only,
                "address": {
                    "street": addr.street, "unit": addr.unit, "city": addr.city,
                    "state": addr.state, "zip": addr.zip,
                    "notes": addr.notes,
                }
                if addr
                else None,
                "members": s.PortalHouseholdMemberSerializer(members, many=True).data,
            }
        )

    def patch(self, request, client_id):
        # Edit the household delivery address.
        enr = self._enrollment(client_id)
        if enr is None:
            return Response(
                {"error": "No active enrollment for this member."},
                status=http.HTTP_404_NOT_FOUND,
            )
        # A superseded (read-only) enrollment is history -- refuse edits.
        if self._is_scoped_override(client_id, enr):
            return Response(
                {"error": "This is a previous enrollment (read-only history)."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Program-tab guard: a closed program (no open internal-service case) is
        # frozen -- refuse every write action.
        if _program_locked(enr):
            return _program_locked_response()
        ser = s.PortalAddressEditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        addr = enr.delivery_address
        previous = timeline._format_address(addr) if addr is not None else ""
        prev_zip = addr.zip if addr is not None else ""
        # Snapshot every editable address field (incl. notes) BEFORE applying, so
        # the timeline logs a precise per-field diff -- and fires even when the
        # one-line address is unchanged but the delivery notes were edited (the
        # old check compared only _format_address, which omits notes).
        ADDR_FIELDS = (
            ("street", "Street"), ("unit", "Unit"), ("city", "City"),
            ("state", "State"), ("zip", "ZIP"), ("notes", "Delivery notes"),
        )
        before = {f: (getattr(addr, f, "") if addr is not None else "") for f, _ in ADDR_FIELDS}
        if addr is None:
            addr = Address.objects.create(client_id=client_id, type="temporary")
            enr.delivery_address = addr
            enr.save(update_fields=["delivery_address"])
        for field, _ in ADDR_FIELDS:
            if field in data:
                setattr(addr, field, data[field])
        addr.save()
        agent = current_agent(request)
        changes = timeline.build_change_list(
            [(label, before[f], getattr(addr, f, "")) for f, label in ADDR_FIELDS]
        )
        if changes:
            try:
                timeline.event_for_delivery_address_change(
                    enr.client, addr, previous=previous, changes=changes,
                    enrollment=enr, actor=_agent_actor(agent),
                )
            except Exception:  # never let history-logging break the edit
                pass
        # Delivery Coverage Eligibility Check on the updated address: flag members
        # Out of Orbit when the new ZIP is out of area; when the ZIP just became
        # serviceable (was excluded, now isn't), return them to Active if the
        # meal rule also passes.
        from ..services.service_area import is_zip_excluded
        new_excluded = is_zip_excluded(addr.zip)
        coverage = None
        if new_excluded:
            coverage = _enforce_delivery_coverage(enr, agent)
        elif is_zip_excluded(prev_zip):
            coverage = _enforce_delivery_coverage(enr, agent, allow_reactivate=True)
        resp = {
            "street": addr.street, "unit": addr.unit, "city": addr.city,
            "state": addr.state, "zip": addr.zip, "notes": addr.notes,
        }
        if coverage and coverage.get("out_of_range"):
            names = coverage["out_of_range"]
            resp["coverage_warning"] = (
                f"ZIP {addr.zip} is outside the delivery coverage area — "
                f"{len(names)} member(s) set Out of Range (excluded from "
                f"deliveries): {', '.join(names)}. The household has been placed "
                f"on hold and a Case Closure ticket opened for review."
            )
        if coverage and coverage.get("reactivated"):
            names = coverage["reactivated"]
            resp["coverage_info"] = (
                f"ZIP {addr.zip} is now serviceable — "
                f"{len(names)} member(s) returned to Active: {', '.join(names)}. "
                f"The household hold was resumed and the Out-of-Range ticket resolved."
            )
        return Response(resp)


class MemberWarningsView(PortalAPIView):
    """GET /members/<client_id>/warnings/ — active care-management warnings for
    the member's profile header.

    Runs a LIVE scan on open (``sync_household_warnings``) so the header is
    always fresh and self-healing, then returns the household's active warnings:
    every household-scope warning plus this member's own member-scope ones."""

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        if enr is None:
            return Response({"warnings": []})
        try:
            sync_household_warnings(enr)
        except Exception:  # never let the snapshot sync break the profile load
            logger.exception("warning sync failed for client %s", client_id)
        rows = (
            MemberWarning.objects.filter(
                enrollment=enr, status=WarningStatus.ACTIVE,
            )
            .filter(Q(scope="household") | Q(client_id=client_id))
            .order_by("-severity", "first_detected_at")
        )
        return Response({"warnings": [
            {
                "code": r.code,
                "severity": r.severity,
                "scope": r.scope,
                "title": r.title,
                "detail": r.detail,
                "context": r.context or {},
                "client_id": str(r.client_id),
                "first_detected_at": r.first_detected_at.isoformat(),
            }
            for r in rows
        ]})


class MemberHouseholdSearchView(PortalAPIView):
    """Search existing clients (by client ID or Medicaid/insurance member ID) to
    add to this member's household. Mirrors the extension's client picker."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        return Response(search_clients(request.query_params.get("q")))


class MemberHouseholdAddView(PortalAPIView):
    """Add an existing client to this member's household. Moves the client out of
    any other household first (one-household-per-client). No family-size cap on
    the CRM -- agents are authoritative."""

    def post(self, request, client_id):
        primary = get_object_or_404(Client, pk=client_id)
        member_id = request.data.get("client_id")
        if not member_id:
            return Response(
                {"error": "client_id is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        try:
            member_client = Client.objects.get(pk=member_id)
        except (Client.DoesNotExist, ValueError):
            return Response(
                {"error": "Client not found."}, status=http.HTTP_404_NOT_FOUND
            )
        if str(member_client.pk) == str(primary.pk):
            return Response(
                {"error": "A client can't be added to their own household."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Program-tab guard: a closed program is frozen -- no roster changes.
        if _program_locked(s.active_enrollment(primary)):
            return _program_locked_response()
        agent = current_agent(request)
        add_client_to_household(primary, member_client, agent=agent)
        actor = _agent_actor(agent)
        try:
            timeline.event_for_household_member_added(
                primary, member_client,
                enrollment=s.active_enrollment(primary), actor=actor,
                added_from="the Household tab",
            )
        except Exception:  # never let history-logging break the add
            pass
        return Response({"client_id": str(member_client.pk)}, status=http.HTTP_201_CREATED)


class MemberInternalCaseDescriptionsView(PortalAPIView):
    """TEMPORARY: the case descriptions of this client's INTERNAL_SERVICE cases,
    surfaced on the Household tab. Read-only. Slated for removal in a few days."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        cases = (
            Case.objects.filter(
                client_id=client_id, case_type=CaseType.INTERNAL_SERVICE,
            )
            .exclude(case_description="")
            .order_by("-date_opened")
        )
        return Response([
            {
                "case_id": str(c.pk),
                "program_name": c.program_name or c.service_type or "",
                "status": c.get_case_status_display() if c.case_status else "",
                "description": c.case_description,
            }
            for c in cases
        ])


class MemberMobileNumberView(PortalAPIView):
    """Add / edit one household member's mobile number on the ACTIVE enrollment
    (the Contacts tab on the program accordion).

    The number is stored on the member's ``MemberDietaryProfile`` (so it is part
    of the verification record and carries across a governing-case replacement)
    and mirrored to ``HouseholdMember.mobile_app_username`` (Benefully app login).
    A blank number is allowed for a DEPENDENT but rejected for the PRIMARY member.
    """

    def patch(self, request, client_id, member_id):
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        if enr is None:
            return Response(
                {"error": "No active enrollment for this member."},
                status=http.HTTP_404_NOT_FOUND,
            )
        if _program_locked(enr):
            return _program_locked_response()
        mv = get_object_or_404(MemberDietaryProfile, pk=member_id, enrollment=enr)
        number = (request.data.get("mobile_number") or "").strip()
        # The primary member (the enrollment's own client) must keep a number.
        if not number and mv.client_id and str(mv.client_id) == str(client.pk):
            return Response(
                {"mobile_number": "A mobile number is required for the primary member."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        mv.mobile_number = number
        mv.save(update_fields=["mobile_number", "updated_at"])
        # Keep the app-login mirror in step for members mapped to a real client.
        if mv.client_id:
            HouseholdMember.objects.filter(client_id=mv.client_id).update(
                mobile_app_username=number,
            )
        return Response(s.MemberDetailSerializer(client).data)


def _promote_removed_member_to_own_household(
    member_client, active_case, *, diet_snapshot, member_name, agent, actor,
):
    """A member removed from a household who still holds an ACTIVE internal-
    service case can't just be dropped -- their meal/box case still needs a
    verification. Make them the PRIMARY of a fresh household and open a
    Pending-Verification enrollment on that case (carrying over their dietary
    data), so they surface on the Verification queue as their own household.

    Assumes the member has already been detached from their prior household
    (their old ``HouseholdMember`` row + dietary profile removed) so
    ``ensure_household_with_primary`` creates a new household with them primary.

    Idempotent-safe: if the member already has a live enrollment, we only ensure
    the household + recompute their stage. Returns the new enrollment (or the
    existing live one) so the caller can report it.
    """
    terminal = (EnrollmentStage.DISREGARDED, EnrollmentStage.CANCELLED)
    household = ensure_household_with_primary(member_client)

    # Already enrolled (their own live enrollment): don't double-create -- just
    # make sure their household + stage are consistent.
    live = (
        member_client.enrollments.exclude(stage__in=terminal)
        .order_by("-opened_at")
        .first()
    )
    if live is not None:
        recompute_enrollment_household(live)
        return live

    # Attach the case only when no other live enrollment already claims it (the
    # per-case unique constraint forbids two live enrollments sharing a case).
    case_free = not active_case.enrollments.exclude(stage__in=terminal).exists()
    program = active_case.program if active_case.program_id else None
    enr = EnrollmentVerification.objects.create(
        client=member_client,
        household=household,
        case=active_case if case_free else None,
        program_name=(program.name if program else "") or (active_case.program_name or ""),
        service_type=active_case.service_type or "",
        household_size=1,
        stage=EnrollmentStage.PENDING_VERIFICATION,
        requested_by=agent,
        requested_at=timezone.now(),
    )
    # Carry the member's dietary snapshot onto the new enrollment so they land on
    # the Household tab with their menu/allergies intact (not reset Out of Orbit).
    MemberDietaryProfile.objects.create(
        enrollment=enr,
        client=member_client,
        member_name=member_name or (
            f"{member_client.first_name} {member_client.last_name}".strip()
        ),
        dietary_restrictions=diet_snapshot.get("dietary_restrictions") or [],
        food_allergies=diet_snapshot.get("food_allergies") or [],
        other_dietary_restrictions=diet_snapshot.get("other_dietary_restrictions") or "",
        meal_category=diet_snapshot.get("meal_category") or "",
        menu_type=diet_snapshot.get("menu_type") or "",
        general_verification_notes=diet_snapshot.get("general_verification_notes") or "",
        status=diet_snapshot.get("status") or MemberStatus.ACTIVE,
    )
    # Drives the whole (new, single-member) household to Pending Verification.
    recompute_enrollment_household(enr)
    try:  # log the verification request on the promoted member's own history
        timeline.event_for_verification(enr, actor=actor)
    except Exception:  # never let history-logging break the removal
        pass
    return enr


class HouseholdMemberEditView(PortalAPIView):
    """PATCH a single household member's dietary info (MemberDietaryProfile)."""

    def patch(self, request, client_id, member_id):
        # Scope the profile to the URL client's ACTIVE enrollment -- the exact
        # set the Household tab (HouseholdView.get) lists. A non-primary member
        # has no enrollment of their own, so active_enrollment() falls back to
        # the household's enrollment (owned by the primary). Filtering by
        # ``enrollment__client_id=client_id`` would 404 for such members, since
        # the enrollment's owner is the primary, not the member being viewed.
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        mv = get_object_or_404(
            MemberDietaryProfile, pk=member_id, enrollment=enr,
        )
        # Program-tab guard: a closed program (no open internal-service case) is
        # frozen -- no dietary edits, pause/unpause, or reactivation.
        if _program_locked(enr):
            return _program_locked_response()
        ser = s.PortalMemberDietaryEditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        # `reactivate` / `deactivate` / `pause` / `unpause` are control flags,
        # not model fields — handle them separately from the dietary fields.
        reactivate = data.pop("reactivate", False)
        deactivate = data.pop("deactivate", False)
        pause = data.pop("pause", False)
        unpause = data.pop("unpause", False)
        restore_range = data.pop("restore_range", False)
        pause_reason = (data.pop("pause_reason", "") or "").strip()
        # Snapshot the dietary fields being edited BEFORE applying them, so we
        # can log a precise before -> after diff on the timeline once saved.
        _DIETARY_LABELS = {
            "dietary_restrictions": "Dietary restrictions",
            "food_allergies": "Food allergies",
            "other_dietary_restrictions": "Other restrictions",
            "meal_category": "Meal category",
            "menu_type": "Menu type",
            "meals_per_delivery": "Meals per delivery",
            "general_verification_notes": "Notes",
        }
        dietary_before = {f: getattr(mv, f) for f in _DIETARY_LABELS if f in data}
        for field, value in data.items():
            setattr(mv, field, value)

        # A locked (pause_locked) member was auto-paused by a Household ->
        # Individual scope switch and cannot be un-paused (or reactivated) here.
        # There are two kinds of lock:
        #   - Manual scope switch (from the Household tab): cleared automatically
        #     by correcting the program scope back to Household.
        #   - Governing-case import mismatch: only a Customer Service dismissal of
        #     the matching Case Mismatch flag can lift it.
        if (unpause or reactivate or restore_range) and mv.pause_locked:
            return Response(
                {"error": (
                    "This member is locked because the program scope is "
                    "Individual and cannot be un-paused here. Switch the program "
                    "scope back to Household to un-pause them (or, if this is a "
                    "case mismatch, Customer Service must dismiss the Case "
                    "Mismatch flag first)."
                )},
                status=http.HTTP_400_BAD_REQUEST,
            )

        if pause and mv.status == MemberStatus.ACTIVE:
            # Manual agent pause (requires a reason). Excludes the member from
            # every delivery schedule / Purchase Order until unpaused.
            mv.status = MemberStatus.PAUSED
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            mv.save()
            agent = current_agent(request)
            actor = _agent_actor(agent)
            try:
                timeline.event_for_member_paused(
                    mv, enrollment=mv.enrollment, reason=pause_reason, actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
            # Agent-authored note with the required reason — NOT a system note.
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.AGENT,
                        author_name=agent.name if agent else "",
                        body=f"Member paused. Reason: {pause_reason}",
                    )
                except Exception:  # never let note-writing break the edit
                    pass
        elif unpause and mv.status == MemberStatus.PAUSED:
            # Lift the manual pause: re-run the kitchen-aware meal rule so the
            # member returns to Active, or falls to Out of Orbit if the current
            # menu/allergies can't be fulfilled by the assigned kitchen. This is
            # the explicit resume flow, so allow_resume=True lets the meal rule
            # move the member OFF the manual PAUSED status.
            reconcile_member_kitchen_output(
                mv, enr.kitchen, save=False, allow_resume=True,
            )
            mv.save()
            agent = current_agent(request)
            actor = _agent_actor(agent)
            try:
                timeline.event_for_member_unpaused(
                    mv, enrollment=mv.enrollment, reason=pause_reason, actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.AGENT,
                        author_name=agent.name if agent else "",
                        body=(
                            f"Member unpaused. Reason: {pause_reason}"
                            if pause_reason else "Member unpaused."
                        ),
                    )
                except Exception:  # never let note-writing break the edit
                    pass
        elif deactivate and mv.status == MemberStatus.ACTIVE:
            # Manual agent override: pull the member Out of Orbit regardless of
            # the meal rule. Clear the kitchen meal result so they're excluded
            # from every delivery schedule / Purchase Order until reactivated.
            mv.status = MemberStatus.OUT_OF_ORBIT
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            mv.save()
            agent = current_agent(request)
            actor = _agent_actor(agent)
            try:
                timeline.event_for_out_of_orbit(
                    mv, enrollment=mv.enrollment,
                    reason="Manually set out of orbit by agent.", actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
            # Leave a system note (same as the auto-out-of-orbit paths),
            # attributed to the acting agent.
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.SYSTEM,
                        author_name=agent.name if agent else "",
                        body=NO_KITCHEN_OUT_OF_ORBIT_NOTE,
                    )
                except Exception:  # never let note-writing break the edit
                    pass
        elif reactivate and mv.status == MemberStatus.OUT_OF_ORBIT:
            # Re-run the kitchen-aware rules against the edited menu type /
            # allergies. Only return the member to Active if the new combination
            # can actually be fulfilled by the household's assigned kitchen;
            # otherwise the agent must pick a different menu type.
            out, _became, reason = reconcile_member_kitchen_output(
                mv, enr.kitchen, save=False,
            )
            if out:
                return Response(
                    {"error": reason or "This menu type can't be fulfilled for this member."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            mv.save()
            agent = current_agent(request)
            actor = _agent_actor(agent)
            try:
                timeline.event_for_member_reactivated(
                    mv, enrollment=mv.enrollment, actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
        elif restore_range and mv.status == MemberStatus.OUT_OF_RANGE:
            # Return an Out-of-Range member to service. Re-check delivery coverage
            # AND the meal rule (reconcile_member_kitchen_output is ZIP-aware): the
            # member is only reactivated when their delivery/primary ZIP is now
            # serviceable (e.g. the ZIP was removed from the excluded list, or the
            # address was corrected) and the kitchen can fulfill them. If the ZIP
            # is still out of coverage the member stays Out of Range and we refuse
            # with a clear reason. Explicit restore-range flow, so allow_resume=
            # True lets the meal rule move the member OFF OUT_OF_RANGE (the ZIP
            # re-check still refuses when the ZIP is still out of coverage).
            out, _became, reason = reconcile_member_kitchen_output(
                mv, enr.kitchen, save=False, allow_resume=True,
            )
            if out:
                return Response(
                    {"error": reason or (
                        "This member's delivery ZIP is still outside the coverage "
                        "area. Update the delivery address (or the excluded-ZIP "
                        "list) before returning them to service."
                    )},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            mv.save()
            agent = current_agent(request)
            actor = _agent_actor(agent)
            try:
                timeline.event_for_member_reactivated(
                    mv, enrollment=mv.enrollment, actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
            # If no member remains Out of Range, resume the auto-hold placed for
            # the out-of-range ZIP and resolve the Out-of-Range closure ticket.
            still_out = enr.member_profiles.filter(
                status=MemberStatus.OUT_OF_RANGE,
            ).exists()
            if not still_out:
                try:
                    _resume_household_after_range(enr)
                except Exception:  # never let the resume break the edit
                    pass
                try:
                    _resolve_out_of_range_tickets(enr, actor=actor)
                except Exception:  # never let ticket resolution break the edit
                    pass
        else:
            # Normal save: reconcile the kitchen output against the global meal
            # rules AND the household's assigned kitchen. An ACTIVE member whose
            # new menu/allergies can't be fulfilled is auto-set Out of Orbit
            # (excluded from schedules/POs). Out-of-orbit members are NOT
            # auto-reactivated here -- that requires the explicit reactivate flag
            # above, so a manual override is never silently undone by an edit.
            became_out = False
            if mv.status == MemberStatus.ACTIVE:
                _out, became_out, reason = reconcile_member_kitchen_output(
                    mv, enr.kitchen, save=False,
                )
            mv.save()
            if became_out:
                agent = current_agent(request)
                actor = _agent_actor(agent)
                try:
                    timeline.event_for_out_of_orbit(
                        mv, enrollment=mv.enrollment,
                        reason=reason or "Menu/allergies can't be fulfilled by the assigned kitchen.",
                        actor=actor,
                    )
                except Exception:  # never let history-logging break the edit
                    pass
                # Leave a customer-facing note explaining why the edit pulled the
                # member Out of Orbit, attributed to the acting agent.
                if mv.client_id:
                    try:
                        Note.objects.create(
                            client=mv.client, source=NoteSource.SYSTEM,
                            author_name=agent.name if agent else "",
                            body=NO_KITCHEN_OUT_OF_ORBIT_NOTE,
                        )
                    except Exception:  # never let note-writing break the edit
                        pass

        # Roll a member pause/unpause up to the PROGRAM: when every household
        # member is paused there is no one left to serve, so the enrollment is
        # placed On Hold; unpausing any member resumes it. Best-effort.
        if pause or unpause:
            try:
                _reconcile_all_paused_hold(enr)
            except Exception:  # never let the program roll-up break the edit
                pass

        # Log the dietary before -> after diff on the member's own history (only
        # when something actually changed). Best-effort: never break the edit.
        dietary_changes = timeline.build_change_list([
            (_DIETARY_LABELS[f], dietary_before[f], getattr(mv, f))
            for f in dietary_before
        ])
        if dietary_changes:
            try:
                timeline.event_for_dietary_changed(
                    mv, changes=dietary_changes, enrollment=mv.enrollment,
                    actor=_agent_actor(current_agent(request)),
                )
            except Exception:  # never let history-logging break the edit
                pass

        # Propagate the edited menu type / allergies onto this member's future
        # SCHEDULED delivery occurrences so PO generation reflects the change
        # (those rows snapshot the profile at calendar-build time). The
        # occurrences are KEPT even when the member is Paused / Out of Orbit /
        # Out of Range -- the delivery calendar overlays that status and PO
        # generation excludes them via the live member-status filter.
        resync_scheduled_orders(enrollment=mv.enrollment)

        # Auto-heal: a member added to an already-in-service household never got
        # a delivery plan (plans are created once, at kitchen assignment), so
        # once they are ACTIVE they would still be missing from the calendar and
        # every future PO. When this edit leaves the member Active WITHOUT a
        # plan -- and the household is already in service (has other plans) --
        # build the missing plan + calendar now. rebuild_delivery_calendar is a
        # no-op for members who already have a plan and preserves PO-batched
        # dates, so it is safe to run on every such save.
        if (
            mv.status == MemberStatus.ACTIVE
            and mv.enrollment_id
            and mv.enrollment.delivery_schedules.exists()
            and not mv.enrollment.delivery_schedules.filter(member_profile=mv).exists()
        ):
            try:
                rebuild_delivery_calendar(mv.enrollment)
            except Exception:  # never let an auto-rebuild break the member edit
                logger.exception(
                    "auto rebuild_delivery_calendar failed after activating "
                    "member %s on enrollment %s", mv.pk, mv.enrollment_id,
                )

        return Response(s.PortalHouseholdMemberSerializer(mv).data)

    def delete(self, request, client_id, member_id):
        """Remove a member from this client's household (Household tab).

        Drops the member's dietary profile on the active enrollment AND their
        household roster row (so the read-side sync won't re-add them),
        mirroring the extension's ``household_remove``. The primary member
        cannot be removed. Logs a 'Household Member Removed' timeline event on
        the primary's history, tagged with the source ("the Household tab").

        If the removed member still holds an ACTIVE internal-service (meal/box)
        case, they can't just be dropped -- their case needs its own
        verification. They're promoted to PRIMARY of a fresh household and a
        Pending-Verification enrollment is opened on that case (see
        :func:`_promote_removed_member_to_own_household`).
        """
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        mv = get_object_or_404(MemberDietaryProfile, pk=member_id, enrollment=enr)

        household = getattr(enr, "household", None)
        if household is None and mv.client_id:
            membership = (
                HouseholdMember.objects.filter(client_id=mv.client_id)
                .select_related("household")
                .first()
            )
            household = membership.household if membership else None

        member_client = mv.client
        # Never remove a primary member -- enforced here for EVERY removal
        # surface routed through this endpoint (the program tab, the Household
        # tab and the verification pop-up). A primary owns their household's
        # timeline + enrollment, so they can't be dropped. Checked directly
        # against the HouseholdMember roster so it holds regardless of which
        # household context the enrollment resolves to.
        if member_client is not None and HouseholdMember.objects.filter(
            client=member_client, is_primary=True
        ).exists():
            return Response(
                {"error": "The primary member cannot be removed."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        member_name = mv.member_name or (
            f"{member_client.first_name} {member_client.last_name}".strip()
            if member_client else ""
        )
        # Snapshot the member's dietary data before we drop their profile, so a
        # promotion (below) can carry it onto their new household's enrollment.
        diet_snapshot = {
            "dietary_restrictions": list(mv.dietary_restrictions or []),
            "food_allergies": list(mv.food_allergies or []),
            "other_dietary_restrictions": mv.other_dietary_restrictions or "",
            "meal_category": mv.meal_category or "",
            "menu_type": mv.menu_type or "",
            "general_verification_notes": mv.general_verification_notes or "",
            "status": mv.status,
        }
        # Does the member being removed still hold an ACTIVE internal-service
        # (meal/box) case? "Active" = an internal-service case that isn't
        # Closed/Cancelled (blank/unknown counts as active, matching the rest of
        # the members query). If so, they get promoted to their own household.
        active_case = None
        if member_client is not None:
            active_case = (
                Case.objects.filter(
                    client=member_client, case_type=CaseType.INTERNAL_SERVICE
                )
                .exclude(case_status__in=(CaseStatus.CLOSED, CaseStatus.CANCELLED))
                .select_related("program")
                .order_by("-date_opened")
                .first()
            )

        # Program-tab guard: a closed program (no open internal-service case) is
        # frozen. Plain removal is refused -- EXCEPT when the member being removed
        # still holds their OWN open internal-service case: they must be extracted
        # to their own household + verification (promotion below), so that path
        # stays open even on a closed household program.
        if _program_locked(enr) and active_case is None:
            return _program_locked_response()

        # The household's primary owns the timeline; the removed-member event is
        # logged on their history (the primary can't themselves be removed -- that
        # is guarded above, against the roster, before any of this runs).
        primary_membership = (
            household.members.filter(is_primary=True).select_related("client").first()
            if household is not None else None
        )
        primary_client = (
            primary_membership.client if primary_membership is not None else client
        )

        agent = current_agent(request)
        actor = _agent_actor(agent)
        promoted = None
        with transaction.atomic():
            if mv.client_id and household is not None:
                HouseholdMember.objects.filter(
                    client_id=mv.client_id, household=household
                ).delete()
                MemberDietaryProfile.objects.filter(
                    client_id=mv.client_id, enrollment__household=household
                ).delete()
            else:
                # Profile-only member (no client link): just drop this profile.
                mv.delete()
            try:
                timeline.event_for_household_member_removed(
                    primary_client, member_client, member_name=member_name,
                    enrollment=enr, actor=actor, removed_from="the Household tab",
                )
            except Exception:  # never let history-logging break the removal
                pass
            # Now detached: promote a member who still has an active internal-
            # service case into their own household + Pending Verification.
            if active_case is not None and member_client is not None:
                promoted = _promote_removed_member_to_own_household(
                    member_client, active_case,
                    diet_snapshot=diet_snapshot, member_name=member_name,
                    agent=agent, actor=actor,
                )

        if promoted is not None:
            return Response(
                {
                    "promoted": True,
                    "enrollment_id": promoted.pk,
                    "stage": promoted.stage,
                    "client_id": str(member_client.pk),
                },
                status=http.HTTP_200_OK,
            )
        return Response(status=http.HTTP_204_NO_CONTENT)


class MemberServiceHoldView(PortalAPIView):
    """Pause the member's household service.

    Moves the active enrollment to On Hold (which logs a StageEvent and mirrors
    a 'Stage changed to On Hold' entry onto the timeline), then records a client
    note with the reason. While On Hold the household is excluded from any new
    Purchase Order until service is resumed.
    """

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        if enr is None:
            return Response(
                {"error": "This member has no active enrollment to place on hold."},
                status=http.HTTP_404_NOT_FOUND,
            )
        # Program-tab guard: a closed program is frozen -- can't be held/resumed.
        if _program_locked(enr):
            return _program_locked_response()
        if EnrollmentStage(enr.stage) == EnrollmentStage.ON_HOLD:
            return Response(
                {"error": "Service is already on hold."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response(
                {"reason": "A reason is required to place service on hold."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        agent = current_agent(request)
        author = agent.name if agent else ""
        try:
            advance_enrollment(
                enr, EnrollmentStage.ON_HOLD,
                note=f"Placed on hold by {author or 'support portal'}. Reason: {reason}",
            )
        except InvalidTransition as exc:
            return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        Note.objects.create(
            client=client, source=NoteSource.AGENT, author_name=author,
            body=f"Service placed on hold. Reason: {reason}",
        )
        return Response(s.MemberDetailSerializer(client).data)


class MemberServiceResumeView(PortalAPIView):
    """Resume a held household.

    Returns the enrollment to the stage it was in before the hold (defaulting to
    Service Active), which logs a StageEvent + timeline entry and re-includes the
    household in Purchase Order batching. Records a client note.
    """

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        if enr is None or EnrollmentStage(enr.stage) != EnrollmentStage.ON_HOLD:
            return Response(
                {"error": "Service is not on hold."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Program-tab guard: a CLOSURE hold (the governing case closed, so no open
        # internal-service case remains) must NOT be manually resumable -- it is
        # the reversible off-ramp that only a NEW open case reopens. Belt-and-
        # suspenders alongside the ``can_resume`` flag that hides the button.
        if _program_locked(enr):
            return _program_locked_response()
        # An ineligibility hold is CareCircle-unfixable: only recovering the
        # underlying data (a later import/save re-running the eligibility gate)
        # may lift it -- a manual Resume must not. Belt-and-suspenders alongside
        # the ``can_resume`` flag that hides the button.
        if _enrollment_resume_blocked_by_ineligibility(enr):
            return Response(
                {"error": (
                    "This program is On Hold because a member is Ineligible, which "
                    "can't be fixed in the CRM. The member's Unite Us case must be "
                    "closed by an agent; service resumes automatically only if the "
                    "eligibility data later passes."
                )},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Resume to the SERVICE stage the enrollment was held from -- but NEVER
        # back into a terminal stage. A household that was REACTIVATED from
        # Cancelled lands in On Hold via a ``cancelled -> on_hold`` StageEvent, so
        # the most-recent hold's ``from_stage`` is ``cancelled``; resuming to that
        # re-cancelled the member on every click (the endless reactivate/resume
        # loop this fixes). Only a genuine service stage (Verified / Kitchen
        # Assignment / Service Active) is a valid resume target -- restrict the
        # lookup to those, and default to Service Active when none is found.
        _RESUMABLE_FROM_STAGES = (
            EnrollmentStage.VERIFIED,
            EnrollmentStage.KITCHEN_ASSIGNMENT,
            EnrollmentStage.SERVICE_ACTIVE,
        )
        last_hold = (
            StageEvent.objects.filter(
                enrollment=enr,
                to_stage=EnrollmentStage.ON_HOLD,
                from_stage__in=[st.value for st in _RESUMABLE_FROM_STAGES],
            )
            .order_by("-entered_at")
            .first()
        )
        target = EnrollmentStage.SERVICE_ACTIVE
        if last_hold and last_hold.from_stage:
            try:
                target = EnrollmentStage(last_hold.from_stage)
            except ValueError:
                target = EnrollmentStage.SERVICE_ACTIVE
        reason = (request.data.get("reason") or "").strip()
        agent = current_agent(request)
        author = agent.name if agent else ""
        suffix = f" Reason: {reason}" if reason else ""
        try:
            # force=True: a prior process gate (e.g. verification) already passed
            # before the hold, so restoring the prior stage must not be re-gated.
            advance_enrollment(
                enr, target, force=True,
                note=f"Service resumed by {author or 'support portal'}.{suffix}",
            )
        except InvalidTransition as exc:
            return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        # Make the resumed household deliverable again -- flipping the enrollment
        # stage back is NOT enough. A cancel/hold can (a) exclude member profiles
        # (INACTIVE), (b) cancel their delivery PLANS, and (c) truncate the plan
        # WINDOW. So, in order:
        #   1. Return each excluded member to service (skipping a manual pause and
        #      an eligibility pause -- those are lifted explicitly).
        #   2. Reactivate any cancelled delivery plans so the calendar can expand.
        #   3. Recompute the plan from the GOVERNING case -- which re-derives the
        #      delivery window from that case's status + authorization window (no
        #      future window => no future deliveries, correctly) -- and rebuild the
        #      dated calendar.
        # Depends entirely on the governing case + auth window, so a member is
        # only made deliverable when their case actually authorizes future service.
        # Best-effort: never fail the resume on it.
        if enr.stage == EnrollmentStage.SERVICE_ACTIVE:
            try:
                from ..models import ScheduleStatus
                from ..services.meal_rules import reconcile_member_kitchen_output
                from ..services.orders import recompute_delivery_plan

                for mv in enr.member_profiles.all():
                    if mv.eligibility_paused or mv.status != MemberStatus.INACTIVE:
                        continue
                    reconcile_member_kitchen_output(
                        mv, kitchen=enr.kitchen, allow_resume=True, save=True,
                    )
                enr.delivery_schedules.filter(
                    status=ScheduleStatus.CANCELLED
                ).update(status=ScheduleStatus.SCHEDULED)
                # Re-derive the plan window from the governing authorization + sync
                # the calendar (falls back to a plain sync when there is no plan).
                recompute_delivery_plan(enr)
                rebuild_delivery_calendar(enr)
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "resume calendar rebuild failed for enrollment %s", enr.pk
                )
        Note.objects.create(
            client=client, source=NoteSource.AGENT, author_name=author,
            body=f"Service resumed.{suffix}",
        )
        return Response(s.MemberDetailSerializer(client).data)


# Restricted maintenance action: only this operator may re-anchor a member as
# the primary of their own household. This is a targeted data-fix for members
# wrongly anchored as a dependent in another household's enrollment (so their
# own case can't be worked); it is not a general agent action.
MAKE_PRIMARY_ALLOWED_EMAIL = "alexis@carecirclecs.com"


class MemberMakePrimaryView(PortalAPIView):
    """POST /members/<client_id>/make-primary/ — make THIS member the primary of
    their own household.

    Splits the member out of any shared household they are currently a
    (non-primary) member of into a fresh household as its primary, moving their
    own enrollments to that household (see
    :func:`ensure_primary_of_own_household`). This is the fix for a member whose
    enrollment is mis-anchored to a relative's household -- the reason they don't
    appear in that household's roster -- so their internal-service case can then
    be worked. Idempotent: a member already primary (or with no household) is a
    no-op re-home.

    Restricted to a single operator (``MAKE_PRIMARY_ALLOWED_EMAIL``); every other
    agent gets 403 and never sees the button.
    """

    def post(self, request, client_id):
        agent = current_agent(request)
        email = (getattr(agent, "email", "") or "").strip().lower()
        if email != MAKE_PRIMARY_ALLOWED_EMAIL:
            return Response(
                {"error": "You are not permitted to perform this action."},
                status=http.HTTP_403_FORBIDDEN,
            )
        client = get_object_or_404(Client, pk=client_id)
        ensure_primary_of_own_household(client)
        # Re-drive the lifecycle stage for the client + their (new) household so
        # the profile header / roster reflect the re-anchoring immediately.
        enr = s.active_enrollment(client)
        if enr is not None:
            recompute_enrollment_household(enr)
        else:
            recompute_client_stage(client)
        author = agent.name if agent else ""
        Note.objects.create(
            client=client, source=NoteSource.AGENT, author_name=author,
            body="Set as primary of their own household (case-fix re-anchor).",
        )
        return Response(s.MemberDetailSerializer(client).data)


class MemberNotesView(PortalGenericAPIView):
    serializer_class = s.PortalNoteSerializer

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        type_filter = (request.query_params.get("type") or "all").lower()
        qs = Note.objects.filter(Q(client_id=client_id) | Q(case__client_id=client_id))
        if type_filter == "client":
            qs = qs.filter(case__isnull=True)
        elif type_filter == "case":
            qs = qs.filter(case__isnull=False)
        qs = qs.distinct()
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        ser = s.PortalNoteCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        agent = current_agent(request)
        note = Note.objects.create(
            client=client,
            case_id=ser.validated_data.get("case_id"),
            source=NoteSource.AGENT,
            author_name=agent.name if agent else "",
            body=ser.validated_data["body"],
        )
        return Response(s.PortalNoteSerializer(note).data, status=http.HTTP_201_CREATED)


class MemberCasesView(PortalAPIView):
    """A member's cases. The lightweight shape powers the New-Ticket “related
    case” dropdown; ``?detail=1`` returns the full shape for the profile's
    Cases tab (authorization, dates, provider, outcome)."""

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        cases = Case.objects.filter(client_id=client_id).order_by("-date_opened")
        if request.query_params.get("detail"):
            # Flag the governing internal-service case the SAME way the stage
            # progress bar stars it (lifecycle.program_tracks), so the Cases tab
            # star always matches the bar.
            from api.services.lifecycle import program_tracks

            tracks = program_tracks(client)
            governing_case_id = next(
                (t["case_id"] for t in tracks if t["governing"]), None
            )
            return Response(
                s.PortalMemberCaseSerializer(
                    cases, many=True,
                    context={"governing_case_id": governing_case_id},
                ).data
            )
        return Response(s.PortalCaseOptionSerializer(cases, many=True).data)


class MemberCaseDetailView(PortalAPIView):
    """Remove a single case from a member's profile. Guardrail: only NON-Met-
    Council cases may be deleted -- these are external orgs' work that shouldn't
    live in the member base. Met Council cases (created OR managed by Met
    Council) are the program's own records and are refused (400) so the source
    of truth stays intact."""

    def delete(self, request, client_id, case_id):
        get_object_or_404(Client, pk=client_id)
        case = get_object_or_404(Case, pk=case_id, client_id=client_id)
        from api.services.lifecycle import case_is_met_council

        if case_is_met_council(case):
            return Response(
                {"detail": "Met Council cases cannot be removed."}, status=400
            )
        case.delete()
        return Response(status=204)


class MemberCaseHistoryView(PortalGenericAPIView):
    """The client timeline scoped to a single case -- the 'Case history' shown on
    the profile's Cases tab. Same event rows as the client history (with the same
    provenance + deep-links), filtered to this case, newest-first + paginated."""

    serializer_class = s.HistoryEventSummarySerializer

    def get(self, request, client_id, case_id):
        get_object_or_404(Client, pk=client_id)
        get_object_or_404(Case, pk=case_id, client_id=client_id)
        qs = TimelineEvent.objects.filter(client_id=client_id, case_id=case_id)
        page = self.paginate_queryset(qs)
        ctx = self.get_serializer_context()
        ctx["actor_names"] = s.build_actor_name_map(page)
        data = self.get_serializer(page, many=True, context=ctx).data
        return self.get_paginated_response(data)


def _enrollment_history_window(enrollment):
    """Resolve the (start, end, open?) bounds of a verification enrollment's
    history timeline.

    Start = the verification completion (``verified_at``), falling back to the
    request / open time so a not-yet-verified enrollment still has a stable
    anchor. End = when the governing internal-service case CLOSED (or was
    cancelled), else the case's authorization approval window end once it is in
    the past, else the enrollment's own close time. ``None`` while the program
    is still live -- in which case ``open`` is True.
    """
    start = enrollment.verified_at or enrollment.requested_at or enrollment.opened_at
    end = None
    case = enrollment.case
    now = timezone.now()
    if case is not None:
        if case.case_status in (CaseStatus.CLOSED, CaseStatus.CANCELLED):
            end = case.case_closed_at or case.updated_at
        elif (
            case.service_authorization_approval_ends_at is not None
            and case.service_authorization_approval_ends_at <= now
        ):
            end = case.service_authorization_approval_ends_at
    if end is None and enrollment.closed_at is not None:
        end = enrollment.closed_at
    return start, end, end is None


class MemberEnrollmentHistoryView(PortalGenericAPIView):
    """The client timeline scoped to a single verification ENROLLMENT -- the
    'History' sub-tab on the profile's Program tab. Mirrors the case history but
    keyed on the enrollment, so it captures the whole household's program
    activity regardless of which member each event is logged on.

    Tracks the program from verification completion through every change made to
    the enrollment (members added / removed, delivery-address edits, out-of-orbit
    / out-of-range flips, dietary & menu edits, stage / authorization changes),
    ending when the authorization window expires or the governing case closes.
    Events are filtered to this enrollment and clamped to that window,
    newest-first."""

    serializer_class = s.HistoryEventSummarySerializer

    def get(self, request, client_id, enrollment_id):
        get_object_or_404(Client, pk=client_id)
        enrollment = get_object_or_404(
            EnrollmentVerification, pk=enrollment_id, client_id=client_id
        )
        start, end, window_open = _enrollment_history_window(enrollment)
        qs = TimelineEvent.objects.filter(enrollment_id=enrollment.pk)
        # Bound to the enrollment's window: from verification completion onward.
        # No hard upper cap -- the program is locked once its case closes, so the
        # terminal (close / expiry) event is the natural last row.
        if start is not None:
            qs = qs.filter(occurred_at__gte=start)
        events = list(
            qs.select_related("content_type", "enrollment")
            .order_by("-occurred_at", "-created_at")
        )
        ctx = self.get_serializer_context()
        ctx["actor_names"] = s.build_actor_name_map(events)
        data = self.get_serializer(events, many=True, context=ctx).data
        return Response({
            "enrollment_id": enrollment.pk,
            "program_name": enrollment.program_name or enrollment.service_type or "",
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
            "window_open": window_open,
            "results": data,
        })


# Noisy / internal fields to hide from the raw field-diff drill-down.
_AUDIT_EXCLUDE = frozenset({
    "updated_at", "created_at", "crm_sync_hash", "crm_synced_at",
})


def _audit_val(v):
    return "" if v is None else str(v)


class MemberCaseAuditView(PortalAPIView):
    """Raw field-level change history for a case, from django-simple-history --
    the 'forensic' drill-down behind the curated Case history. Each entry lists
    the fields that changed (old -> new) with who/where (change_source/actor)."""

    def get(self, request, client_id, case_id):
        get_object_or_404(Client, pk=client_id)
        case = get_object_or_404(Case, pk=case_id, client_id=client_id)
        records = list(case.history.all())  # newest first
        out = []
        for i, rec in enumerate(records):
            prev = records[i + 1] if i + 1 < len(records) else None
            entry = {
                "changed_at": rec.history_date,
                "source": rec.change_source or "",
                "actor": rec.change_actor or "",
                "type": rec.get_history_type_display(),
                "changes": [],
            }
            if prev is not None:
                try:
                    delta = rec.diff_against(prev, excluded_fields=_AUDIT_EXCLUDE)
                except Exception:  # noqa: BLE001 - never fail the audit view
                    delta = None
                if delta is not None:
                    entry["changes"] = [
                        {
                            "field": c.field,
                            "old": _audit_val(c.old),
                            "new": _audit_val(c.new),
                        }
                        for c in delta.changes
                    ]
                    if not entry["changes"]:
                        continue  # unchanged snapshot -> skip
            out.append(entry)
        return Response({"results": out})


class MemberTicketsView(PortalAPIView):
    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = (
            Ticket.objects.filter(client_id=client_id)
            .select_related("assigned_to", "client", "case", "type")
            .prefetch_related("notes")
        )
        # ?mine=true -> only tickets assigned to the requesting agent.
        mine = (request.query_params.get("mine") or "").strip().lower()
        if mine in ("1", "true", "yes"):
            agent = current_agent(request)
            qs = qs.filter(assigned_to=agent) if agent else qs.none()
        return Response(s.PortalTicketSerializer(qs, many=True).data)


class MemberVerificationCreateView(PortalAPIView):
    """POST: create an EnrollmentVerification + MemberDietaryProfiles + delivery
    Address for a member (the 5-step wizard).

    On save the household is verified: ``verified_at``/``verified_by`` are set
    (the source-of-truth verification fact) and the enrollment advances to
    VERIFIED (driving the client to the "Verified" lifecycle stage). When the
    governing case authorization is "Accepted" the enrollment is advanced to
    KITCHEN_ASSIGNMENT (awaiting the manual kitchen-assignment step, which builds
    the delivery schedule). Each transition is recorded on the client's history
    (StageEvent + timeline event).
    """

    @transaction.atomic
    def post(self, request, client_id):
        from ..services.williamsburg import (
            WILLIAMSBURG_KITCHEN_NAME,
            WILLIAMSBURG_MENU_TYPE,
        )

        client = get_object_or_404(Client, pk=client_id)
        # Only a member who OWNS an (open) Internal Service case can be verified --
        # the verification + meal/box delivery attach to that case. A member who
        # doesn't hold their own internal-service case (e.g. a dependent) can't be
        # the SUBJECT of a verification, even if the pop-up is somehow opened on
        # them. Mirrors the same gate on MemberRequestVerificationView.
        if not has_open_internal_service_case(client):
            return Response(
                {"error": "This member doesn't own an open Internal Service case, "
                          "so they can't be verified."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        ser = s.VerificationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        # Williamsburg exception (lead source == "Williamsburg"): the whole
        # household is served the Kosher menu and, on save, the Williamsburg
        # kitchen is auto-assigned and service activated directly (see below).
        is_williamsburg = bool(getattr(client, "is_williamsburg", False))

        # The agent may pick WHICH Internal Service case this verification is tied
        # to in the pop-up (the client can hold several). Resolve it here; falls
        # back to the client's governing case below when not provided.
        selected_case = None
        sel_case_id = request.data.get("case_id")
        if sel_case_id:
            selected_case = next(
                (
                    c
                    for c in client.cases.all()
                    if str(c.case_id) == str(sel_case_id)
                    and c.case_type == CaseType.INTERNAL_SERVICE
                ),
                None,
            )

        # De-duplicate members by client_id (first occurrence wins). The same
        # person can be submitted twice -- e.g. the primary is auto-included AND
        # re-added via the Step-1 search -- which would violate the per-
        # (enrollment, client) unique constraint on MemberDietaryProfile and
        # raise an IntegrityError (500). Members without a client_id are kept
        # as-is (a NULL client doesn't participate in the unique constraint).
        seen_member_ids = set()
        deduped_members = []
        for m in data["members"]:
            cid = m.get("client_id")
            if cid is not None:
                key = str(cid)
                if key in seen_member_ids:
                    continue
                seen_member_ids.add(key)
            deduped_members.append(m)
        data["members"] = deduped_members

        # A mobile number is REQUIRED for the primary member (the household's
        # point of contact); dependents are optional. The primary is the member
        # whose client_id is this client (or the first row when unmapped).
        def _is_primary_row(m, idx):
            cid = m.get("client_id")
            return (str(cid) == str(client.pk)) if cid else (idx == 0)

        primary_row = next(
            (m for i, m in enumerate(data["members"]) if _is_primary_row(m, i)),
            (data["members"][0] if data["members"] else None),
        )
        if primary_row is not None and not (primary_row.get("mobile_number") or "").strip():
            return Response(
                {"mobile_number": "A mobile number is required for the primary member."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # Delivery address (shared by the household). Unit/apt is stored in its
        # own field so the kitchen + delivery label can show it distinctly.
        address = Address.objects.create(
            client=client,
            type="temporary",
            street=data.get("street", ""),
            unit=data.get("apt", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            zip=data.get("zip", ""),
            notes=data.get("address_notes", ""),
        )

        household = getattr(
            getattr(client, "household_membership", None), "household", None
        )
        # Members the agent added via the Step-1 search carry a client_id that
        # isn't the primary. If any are present we need a household to attach
        # them to, so create one (with the primary) when the client has none.
        extra_member_ids = [
            str(m["client_id"])
            for m in data["members"]
            if m.get("client_id") and str(m["client_id"]) != str(client.pk)
        ]
        if extra_member_ids and household is None:
            household = ensure_household_with_primary(client)
        # Start at PENDING_VERIFICATION; the guarded lifecycle transitions below
        # move it forward and write the history rows. The agent running the
        # wizard both requests and (below) completes the verification.
        acting_agent = current_agent(request)
        enrollment = EnrollmentVerification.objects.create(
            client=client,
            household=household,
            program_name=data.get("program_name", ""),
            delivery_address=address,
            delivery_weekdays=data.get("delivery_weekdays", []),
            household_size=len(data["members"]),
            is_family_verified=data.get("is_family_verified"),
            medicaid_type_verified=data.get("medicaid_type_verified"),
            delivery_address_verified=data.get("delivery_address_verified"),
            stage=EnrollmentStage.PENDING_VERIFICATION,
            requested_by=acting_agent,
        )

        for m in data["members"]:
            MemberDietaryProfile.objects.create(
                enrollment=enrollment,
                client_id=m.get("client_id"),
                member_name=m.get("member_name", ""),
                dietary_restrictions=m.get("dietary_restrictions", []),
                food_allergies=m.get("food_allergies", []),
                other_dietary_restrictions=m.get("other_dietary_restrictions", ""),
                meal_category=m.get("meal_category", ""),
                # Williamsburg households are always served the Kosher menu (the
                # agent's allergies/restrictions are still honored). Otherwise the
                # menu type is derived from the member's dietary data (allergy
                # overrides win, else meal_category) when not explicitly sent.
                menu_type=(
                    WILLIAMSBURG_MENU_TYPE
                    if is_williamsburg
                    else m.get("menu_type")
                    or menu_type_for_member(
                        food_allergies=m.get("food_allergies", []),
                        meal_category=m.get("meal_category", ""),
                    )
                ),
                general_verification_notes=m.get("notes", ""),
                mobile_number=(m.get("mobile_number") or "").strip(),
            )

            # Wire the member's mobile-app login number onto their HouseholdMember
            # row (the field powers the Benefully member app login). Only members
            # that map to a real client/household-member can be wired here.
            mobile = (m.get("mobile_number") or "").strip()
            member_client_id = m.get("client_id")
            if mobile and member_client_id:
                HouseholdMember.objects.filter(
                    client_id=member_client_id
                ).update(mobile_app_username=mobile)

        # Attach any members added via the Step-1 search to the household and
        # record each addition on the primary's timeline. Skip clients already
        # in another household (one-household-per-client) and existing members
        # of THIS household (no duplicate row, no duplicate timeline event).
        if household is not None and extra_member_ids:
            agent = current_agent(request)
            actor = _agent_actor(agent)
            for mid in extra_member_ids:
                member_client = Client.objects.filter(pk=mid).first()
                if member_client is None:
                    continue
                membership = HouseholdMember.objects.filter(
                    client=member_client
                ).first()
                if membership is not None:
                    continue  # already in a household (this or another) — leave it
                HouseholdMember.objects.create(
                    household=household, client=member_client, is_primary=False
                )
                try:
                    timeline.event_for_household_member_added(
                        client, member_client, enrollment=enrollment, actor=actor,
                        added_from="the verification pop-up",
                    )
                except Exception:  # never let history-logging break the save
                    pass

        # Completing the wizard IS the verification: stamp the source-of-truth
        # fact (verified_at/verified_by), then force past the process gate. This
        # records a StageEvent + timeline event and recomputes the client's
        # lifecycle stage to "Verified".
        agent = current_agent(request)
        enrollment.verified_at = timezone.now()
        enrollment.verified_by = agent
        enrollment.save(update_fields=["verified_at", "verified_by"])
        # Record WHO verified on the history timeline + StageEvent audit. The
        # portal actor is an Agent (not a User), so pass it as a display label.
        actor_label = (
            agent.name or (f"agent:{agent.agent_code}" if agent.agent_code else "")
        ) if agent else ""
        advance_enrollment(
            enrollment, EnrollmentStage.VERIFIED, force=True,
            actor_label=actor_label,
            note="Verification completed via support portal.",
        )

        # Summary event capturing WHAT was verified -- the household roster + each
        # member's resolved menu, the delivery address/days, and which checkboxes
        # the agent confirmed -- so the History detail shows the verification data,
        # not just the bare stage change. De-duped per enrollment.
        try:
            addr_str = ", ".join(
                p for p in [
                    address.street, address.unit, address.city,
                    " ".join(x for x in [address.state, address.zip] if x),
                ] if p
            )
            submitted_members = [
                {"member_name": mp.member_name, "menu_type": mp.menu_type}
                for mp in enrollment.member_profiles.all()
            ]
            timeline.event_for_verification_submitted(
                enrollment,
                members=submitted_members,
                delivery_address=addr_str,
                delivery_weekdays=data.get("delivery_weekdays", []),
                verified_flags={
                    "family": data.get("is_family_verified"),
                    "medicaid_type": data.get("medicaid_type_verified"),
                    "delivery_address": data.get("delivery_address_verified"),
                },
                actor=actor_label,
            )
        except Exception:  # never let history-logging break the verification
            pass

        # Tie the enrollment to the agent-selected Internal Service case from the
        # pop-up (when provided + free) BEFORE the authorization projection, so
        # the switch sticks even while the case is still pending. A case already
        # owned by another enrollment is skipped (per-case unique constraint).
        if (
            selected_case is not None
            and enrollment.case_id is None
            and not EnrollmentVerification.objects.filter(case=selected_case)
            .exclude(pk=enrollment.pk)
            .exists()
        ):
            enrollment.case = selected_case
            enrollment.save(update_fields=["case"])
            try:
                timeline.event_for_verification_case_switched(
                    enrollment, previous_case=None, actor=actor_label
                )
            except Exception:  # never let history-logging break the save
                pass

        # Snapshot the scope (Household / Individual) this household was VERIFIED
        # under onto the enrollment itself. This is the enrollment's own scope --
        # the governing case's household_type is NEVER changed; it stays the
        # authoritative baseline. If a DIFFERENT governing case is later attached
        # (e.g. an Individual case replacing the Household one this was verified
        # for), the two diverge and the Household tab surfaces the mismatch so an
        # agent can reconcile it. Derived from the tied/governing case now.
        gov_at_verify = governing_internal_case(enrollment) or enrollment.case or selected_case
        if gov_at_verify is not None and not enrollment.household_type_override:
            enrollment.household_type_override = (
                gov_at_verify.household_type or CaseHouseholdType.INDIVIDUAL
            )
            enrollment.save(update_fields=["household_type_override"])

        # Snapshot the VERIFIED product kind (Meals / Boxes) onto the enrollment,
        # same rationale as the household scope above: the governing case's
        # classification stays the baseline; a later divergence surfaces as a
        # mismatch to reconcile. Derived from the tied/governing case now.
        if not enrollment.product_type_override_id:
            verified_kind = (
                detected_product_kind_for_enrollment(enrollment)
                or product_kind_for_enrollment(enrollment)
            )
            if verified_kind is not None:
                pt = ProductType.objects.filter(type=verified_kind).first()
                if pt is not None:
                    enrollment.product_type_override = pt
                    enrollment.save(update_fields=["product_type_override"])

        # Branch the post-verification projection. Williamsburg households are
        # fast-tracked to Service Active with the Williamsburg kitchen; everyone
        # else goes through the standard authorization projection (which uses the
        # single canonical chokepoint reconcile_enrollment_authorization to stay
        # in lock-step with the nightly Unite Us import and the
        # reconcile_authorizations backfill, and reuses the per-case guard).
        wburg_kitchen = (
            Kitchen.objects.filter(name__iexact=WILLIAMSBURG_KITCHEN_NAME).first()
            if is_williamsburg
            else None
        )
        if is_williamsburg and wburg_kitchen is not None:
            # Williamsburg exception: skip the manual kitchen-assignment step
            # entirely. Auto-assign the Williamsburg kitchen and activate service
            # directly (Mon/Thu). assign_kitchen_to_household applies the kitchen
            # meal rules per member (Kosher menu + any "X Free" allergy notes),
            # builds the delivery plan + calendar and advances to Service Active.
            assign_kitchen_to_household(
                enrollment, client, wburg_kitchen,
                cadence=DeliveryCadence.MON_THU, agent=agent,
            )
        else:
            # Standard flow: project the case authorization onto the stage. An
            # APPROVED governing internal-service case advances the verified
            # household to "Kitchen Assignment"; pending/denied/expired leaves it
            # at "Verified" (the outcome is shown separately, from the Case). The
            # member is NOT auto-activated here -- that happens later at the manual
            # kitchen-assignment step. (Falls back here for a Williamsburg client
            # too when no Williamsburg kitchen is configured.)
            reconcile_enrollment_authorization(
                enrollment,
                actor_label=actor_label,
                note="Authorization accepted — awaiting kitchen assignment.",
            )

        # Delivery Coverage Eligibility Check (LAST, so its side effects are the
        # final state): the verification still completes, but any member whose
        # delivery/primary ZIP is outside the coverage area is set Out of Range
        # (system note + timeline event), the whole household is placed On Hold,
        # and a Case Closure ticket is opened for review. Running this after the
        # authorization/kitchen projection ensures an accepted-auth advance can't
        # pull the household back out of the auto-hold.
        _enforce_delivery_coverage(enrollment, agent)
        enrollment.refresh_from_db()

        # Eligibility node: with the household verified and the delivery/primary
        # coverage check run, evaluate each member's HARD eligibility gates
        # (out-of-range PRIMARY address, wrong Medicaid type MLTC/MAP/FFS,
        # missing/expired medical insurance) and set the Ineligible node label +
        # note/timeline. reconcile_client_eligibility is client-scoped, so run it
        # once per household member. (The enrollment DELIVERY-address gate and the
        # recovery-on-fix path are wired on the ext save / client CSV import, not
        # here.) Best-effort: never let it break the verification.
        from api.history import ChangeSource
        from ..services.eligibility import reconcile_client_eligibility

        seen_client_ids = set()
        member_clients = [enrollment.client] + [
            mp.client
            for mp in enrollment.member_profiles.select_related("client").all()
        ]
        for member_client in member_clients:
            if member_client is None or member_client.pk in seen_client_ids:
                continue
            seen_client_ids.add(member_client.pk)
            try:
                reconcile_client_eligibility(
                    member_client, actor_label=actor_label, source=ChangeSource.CRM,
                )
            except Exception:  # never let eligibility break the verification
                pass
        enrollment.refresh_from_db()

        return Response(
            {"id": enrollment.pk, "code": enrollment.code, "stage": enrollment.stage},
            status=http.HTTP_201_CREATED,
        )


class MemberVerificationDisregardView(PortalAPIView):
    """POST: disregard a member's PENDING verification request.

    An agent opening the verification pop-up can DISMISS a request that shouldn't
    be pursued (e.g. the member was requested in error). Two shapes of "pending"
    are handled:

    * A governing EnrollmentVerification at ``PENDING_VERIFICATION`` is moved to
      the non-terminal ``Disregarded`` stage -- its dietary profiles, delivery
      address and case link are KEPT for history but it stops governing the
      client's lifecycle stage. A future request (from the ext) creates a NEW
      enrollment.
    * A client with NO enrollment whose ``lifecycle_stage`` was set to
      pending_verification directly (e.g. by the data import) is simply reverted
      to their derived pre-verification funnel stage.

    Either way the member drops off the Verification page. A reason is REQUIRED
    and recorded on the client's notes and timeline/history.
    """

    @transaction.atomic
    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        # A member is "pending verification" in one of two ways:
        #   1. A real EnrollmentVerification is at the PENDING_VERIFICATION stage
        #      (a request raised from the ext). We move that row to Disregarded.
        #   2. No enrollment exists, but the client's lifecycle_stage was set to
        #      pending_verification directly (e.g. by the data import). There is
        #      no request row to move, so we simply revert the client's stage.
        # Both must be dismissible, since the Verification page shows both.
        enr = governing_pending_enrollment(client)
        is_stage_only_pending = (
            enr is None and client.lifecycle_stage == ClientStage.PENDING_VERIFICATION
        )
        if enr is None and not is_stage_only_pending:
            return Response(
                {"error": "This member has no pending verification request to disregard."},
                status=http.HTTP_409_CONFLICT,
            )
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response(
                {"reason": "A reason is required to disregard a verification request."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        agent = current_agent(request)
        author = agent.name if agent else ""
        actor_label = (
            agent.name or (f"agent:{agent.agent_code}" if agent.agent_code else "")
        ) if agent else ""

        if enr is not None:
            # Case 1: move the governing pending enrollment to Disregarded. The
            # row (dietary profiles, delivery address, case link) is KEPT for
            # history; advance_enrollment logs a StageEvent + timeline event and
            # recomputes the household so every member leaves the window.
            note = (
                f"Verification request disregarded by {author or 'support portal'}. "
                f"Reason: {reason}"
            )
            enr.note = f"{enr.note}\n{note}" if enr.note else note
            enr.save(update_fields=["note"])
            try:
                advance_enrollment(
                    enr, EnrollmentStage.DISREGARDED,
                    actor_label=actor_label, note=note,
                )
            except InvalidTransition as exc:
                return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        else:
            # Case 2: no enrollment -- the pending state lives only on the client's
            # lifecycle_stage. Revert it to the derived (pre-verification) funnel
            # stage, which drops the member off the Verification page, and record a
            # timeline entry carrying the reason (there's no StageEvent note path
            # here, so emit the event directly).
            # actor is a Django User FK on StageEvent (not our Agent); agent
            # attribution is captured on the Note + timeline event instead.
            recompute_client_stage(client)
            timeline.emit_timeline_event(
                client=client,
                event_type=TimelineEventType.VERIFICATION_DISREGARDED,
                occurred_at=timezone.now(),
                title="Verification Request Disregarded",
                subtitle=f"Reason: {reason}",
                badge_tone=TimelineBadgeTone.WARNING,
                source="portal",
                actor=actor_label,
            )

        # Audit stamp: record WHEN this member's request was last disregarded.
        # (The Verification button visibility is driven by whether a pending
        # request exists, not by this field.)
        client.verification_disregarded_at = timezone.now()
        client.save(update_fields=["verification_disregarded_at"])

        Note.objects.create(
            client=client, source=NoteSource.AGENT, author_name=author,
            body=f"Verification request disregarded. Reason: {reason}",
        )
        return Response(s.MemberDetailSerializer(client).data)


class MemberDismissAttentionView(PortalAPIView):
    """POST: dismiss a client from the Urgent Care ("Need Attention") list.

    Clears the ``is_new`` flag so a client that no longer needs verification
    attention (e.g. verified through another path, or flagged in error) drops
    off the list. Normally ``is_new`` clears automatically when a verification
    completes; this is the manual escape hatch for the stragglers that aren't
    pending verification. Idempotent (a no-op when already clear). Records an
    audit Note."""

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        if client.is_new:
            client.is_new = False
            client.save(update_fields=["is_new"])
            agent = current_agent(request)
            author = agent.name if agent else ""
            Note.objects.create(
                client=client, source=NoteSource.AGENT, author_name=author,
                body="Dismissed from the Urgent Care list.",
            )
        return Response(s.MemberDetailSerializer(client).data)


class MemberRequestVerificationView(PortalAPIView):
    """POST: request a verification for a brand-new Urgent Care client.

    Creates the Pending-Verification enrollment (mirroring the extension's
    E-Form request) so the whole household moves to Pending Verification and the
    client drops off the Urgent Care list. HARD-GATED on the same conditions
    that flag ``is_new`` (``is_urgent_care_candidate``): an open internal-service
    case, no verification requested yet, and valid Medicaid + social care
    coverage. Returns 400 with the specific missing prerequisites otherwise."""

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)

        # Gate: reject with the specific missing prerequisite(s) so the UI can
        # explain why. Mirrors is_urgent_care_candidate but itemized.
        missing = []
        if not has_open_internal_service_case(client):
            missing.append("no open Internal Service case")
        if not has_valid_medicaid(client):
            missing.append("no valid Medicaid insurance")
        if not has_valid_social_care(client):
            missing.append("no valid social care coverage")
        if missing:
            return Response(
                {"error": "Can't request verification: " + ", ".join(missing) + "."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Already requested/handled (own or household enrollment) -- nothing to do.
        if not is_urgent_care_candidate(client):
            return Response(
                {"error": "A verification has already been requested for this client."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        agent = current_agent(request)
        actor = _agent_actor(agent)
        terminal = (EnrollmentStage.DISREGARDED, EnrollmentStage.CANCELLED)
        with transaction.atomic():
            household = ensure_household_with_primary(client)
            case = s.internal_service_case(client)
            case_free = case is not None and not case.enrollments.exclude(
                stage__in=terminal
            ).exists()
            program = case.program if (case and case.program_id) else None
            enr = EnrollmentVerification.objects.create(
                client=client,
                household=household,
                case=case if case_free else None,
                program_name=(program.name if program else "")
                or (case.program_name if case else ""),
                service_type=(case.service_type if case else "") or "",
                household_size=household.members.count() or 1,
                stage=EnrollmentStage.PENDING_VERIFICATION,
                requested_by=agent,
                requested_at=timezone.now(),
            )
            # Drives the whole household to Pending Verification and drops the
            # primary off the Urgent Care list (clears is_new).
            recompute_enrollment_household(enr)
            clear_new_flag_on_verification_request(enr)
            try:
                timeline.event_for_verification(enr, actor=actor)
            except Exception:  # never let history-logging break the request
                logger.warning("request-verification timeline emit failed", exc_info=True)

        client.refresh_from_db()
        return Response(s.MemberDetailSerializer(client).data)


def _logistics_enrollment(client_id):
    """The active enrollment for a member, or (None, error_response)."""
    client = get_object_or_404(Client, pk=client_id)
    enr = s.active_enrollment(client)
    if enr is None:
        return None, None, Response(
            {"error": "No active enrollment for this member."},
            status=http.HTTP_404_NOT_FOUND,
        )
    return client, enr, None


def assign_kitchen_to_household(
    enr, client, kitchen, *, cadence, once_weekday=None,
    member_quantities=None, exclude_notes=None, agent=None,
):
    """Assign ``kitchen`` + ``cadence`` to a whole household, apply the kitchen
    output rules to every member, build the delivery plan + calendar, and
    activate service (Service Active).

    Shared by the single-household Logistics assignment and the bulk boxes
    assignment so the meal/kitchen output rules are applied identically. The
    caller must have validated ``kitchen`` and ``cadence`` first.

    ``exclude_notes`` maps a MemberDietaryProfile pk -> a customer-facing note
    for members the agent manually pulled Out of Orbit (the override wins over
    the meal rule). Returns a summary dict for reporting.
    """
    member_quantities = member_quantities or {}
    exclude_notes = exclude_notes or {}
    actor = _agent_actor(agent)

    # Capture the pre-assignment kitchen + cadence so a RE-assignment (the
    # household already had a kitchen) logs a precise 'Kitchen Changed' diff.
    # First-time assignment (no previous kitchen) is already recorded by the
    # KITCHEN_ASSIGNED stage event, so we skip the change row there.
    previous_kitchen = enr.kitchen.name if enr.kitchen_id else ""
    previous_cadence = current_household_cadence(enr) or ""

    enr.kitchen = kitchen
    enr.save(update_fields=["kitchen"])

    # Apply the Meal Rules to each member: derive the kitchen meal type + food
    # notes (sent to the kitchen on the PO) or flag the member Out of Orbit.
    # Reconciliation is kitchen-aware, so a member the CHOSEN kitchen can't
    # fulfill (menu not offered / allergy it can't handle) is also set Out of
    # Orbit. Out-of-orbit members are excluded from schedules + POs.
    offered = kitchen_offered_menu_index(kitchen)
    out_of_orbit = 0
    out_names = []          # ACTIVE -> OUT_OF_ORBIT this run (new kitchen can't serve)
    reactivated_names = []  # OUT_OF_ORBIT -> ACTIVE this run (new kitchen can serve)

    def _member_name(p):
        c = getattr(p, "client", None)
        name = f"{getattr(c, 'first_name', '')} {getattr(c, 'last_name', '')}".strip()
        return name or p.member_name or (str(c.pk) if c else "Member")

    for profile in enr.member_profiles.select_related("client").all():
        was_out = profile.status == MemberStatus.OUT_OF_ORBIT
        if profile.pk in exclude_notes:
            # Manual exclusion: force Out of Orbit and drop the kitchen meal
            # result so they're excluded from schedules + POs, regardless of
            # what the meal rule would decide.
            note = exclude_notes[profile.pk]
            profile.status = MemberStatus.OUT_OF_ORBIT
            profile.kitchen_meal_type = ""
            profile.kitchen_food_notes = ""
            profile.save(update_fields=[
                "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
            ])
            out_of_orbit += 1
            reason = note or "Excluded from kitchen assignment by agent."
            try:
                timeline.event_for_out_of_orbit(
                    profile, enrollment=enr, reason=reason, actor=actor,
                )
            except Exception:  # never let history-logging break assignment
                pass
            # Add a customer-facing note on the member's own client record.
            if note and profile.client_id:
                try:
                    Note.objects.create(
                        client=profile.client, source=NoteSource.AGENT,
                        author_name=agent.name if agent else "", body=note,
                    )
                except Exception:  # never let note-writing break assignment
                    pass
            continue
        _out, became_out, reason = reconcile_member_kitchen_output(
            profile, kitchen, offered=offered,
        )
        if profile.status == MemberStatus.OUT_OF_ORBIT:
            out_of_orbit += 1
            if became_out:
                out_names.append(_member_name(profile))
        elif was_out:
            # The new kitchen (and a serviceable ZIP) can now fulfill a member
            # who was previously Out of Orbit -> returned to Active.
            reactivated_names.append(_member_name(profile))
            try:
                timeline.event_for_member_reactivated(
                    profile, enrollment=enr, actor=actor,
                )
            except Exception:  # never let history-logging break assignment
                pass
        if became_out:
            try:
                timeline.event_for_out_of_orbit(
                    profile, enrollment=enr,
                    reason=reason or "Allergy/menu combination cannot be safely fulfilled.",
                    actor=actor,
                )
            except Exception:  # never let history-logging break assignment
                pass
            # Note explaining why the assigned kitchen couldn't serve them,
            # attributed to the acting agent (blank for unattended bulk runs).
            if profile.client_id:
                try:
                    Note.objects.create(
                        client=profile.client, source=NoteSource.SYSTEM,
                        author_name=agent.name if agent else "",
                        body=NO_KITCHEN_OUT_OF_ORBIT_NOTE,
                    )
                except Exception:  # never let note-writing break assignment
                    pass

    case = enr.case or s.primary_case(client)
    created = create_member_delivery_schedules(
        enr, case=case, cadence=cadence, once_a_week_weekday=once_weekday,
        kitchen=kitchen, member_quantities=member_quantities,
    )

    if created:
        # First-time plan: expand the per-member plans into the dated delivery
        # calendar (OrderSchedule) so the household shows up for PO generation.
        generate_delivery_calendar(enr)
    else:
        # Re-assignment: the household ALREADY had a plan, so the builder above
        # was a no-op and would otherwise keep the OLD cadence's delivery days.
        # Re-apply the chosen cadence to the existing schedules (recomputes
        # weekdays, first delivery, per-delivery quantity + totals) and rebuild
        # the dated calendar so delivery DATES move with the cadence.
        update_household_cadence(
            enr, cadence=cadence, once_a_week_weekday=once_weekday, case=case,
            product_kind=product_kind_for_enrollment(enr),
        )
        sync_delivery_calendar(enr)

    # Push the newly chosen kitchen + refreshed meal-rule results onto the plans
    # and future occurrences so PO generation reflects the change.
    enr.delivery_schedules.update(kitchen=kitchen)
    resync_scheduled_orders(enrollment=enr)

    # A household reaching this helper straight from VERIFIED (the Williamsburg
    # fast-track in the verification pop-up, which skips the manual Logistics
    # kitchen-assignment step) must pass through KITCHEN_ASSIGNMENT first: the
    # transition map has no VERIFIED -> SERVICE_ACTIVE edge, so jumping directly
    # raises InvalidTransition even with force=True. From the Logistics page the
    # enrollment is already at KITCHEN_ASSIGNMENT, so this is a no-op there.
    if EnrollmentStage(enr.stage) == EnrollmentStage.VERIFIED:
        advance_enrollment(
            enr, EnrollmentStage.KITCHEN_ASSIGNMENT, force=True,
            note=f"Kitchen assigned ({kitchen.name}).",
        )

    advance_enrollment(
        enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
        note=f"Kitchen assigned ({kitchen.name}); service activated.",
    )

    # Reconcile the warning snapshot now that a kitchen + cadence are assigned:
    # a household that was flagged "No kitchen assigned" / "No cadence assigned"
    # no longer matches those checks, so their stale ACTIVE MemberWarning rows
    # are resolved here instead of lingering on Care Management as a false
    # positive until the nightly sweep. Best-effort -- never break assignment.
    try:
        sync_household_warnings(enr)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "warning sync failed after kitchen assignment for enrollment %s", enr.pk
        )

    # Log the kitchen/cadence change on a RE-assignment (skipped on first-time
    # assignment, where previous_kitchen is blank). event_for_kitchen_changed is
    # itself a no-op when nothing actually changed. Best-effort.
    if previous_kitchen:
        try:
            _cad_label = {c.code: c.label for c in Cadence.objects.all()}
            timeline.event_for_kitchen_changed(
                enr,
                previous_kitchen=previous_kitchen,
                new_kitchen=kitchen.name if kitchen else "",
                previous_cadence=_cad_label.get(previous_cadence, previous_cadence),
                new_cadence=_cad_label.get(cadence, cadence),
                actor=actor,
            )
        except Exception:  # never let history-logging break assignment
            pass

    return {
        "out_of_orbit": out_of_orbit,
        "out_names": out_names,
        "reactivated_names": reactivated_names,
    }


class MemberKitchenOptionsView(PortalAPIView):
    """Logistics: the household's members (read-only dietary), the available
    kitchens with per-member coverage warnings, cadence options and the
    authorization window — everything needed to assign a kitchen."""

    def get(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        data = kitchen_options(enr)
        case = enr.case or s.primary_case(client)
        window = {"starts_on": None, "ends_on": None}
        if case is not None:
            starts = case.service_authorization_approval_starts_at
            ends = case.service_authorization_approval_ends_at
            window = {
                "starts_on": starts.date().isoformat() if starts else None,
                "ends_on": ends.date().isoformat() if ends else None,
            }
        data["enrollment"] = {
            "id": enr.pk,
            "code": enr.code,
            "stage": enr.stage,
            "program_name": enr.program_name,
            "kitchen_id": str(enr.kitchen_id) if enr.kitchen_id else None,
        }
        data["cadence_options"] = cadence_options_for_kind(data.get("product_kind"))
        data["window"] = window
        return Response(data)


class MemberAssignKitchenView(PortalAPIView):
    """Logistics: assign a kitchen + cadence to the whole household, build the
    per-member delivery plans, and activate the household (Service Active).

    PO generation stays a separate manual step. Body:
    ``{kitchen_id, cadence, once_a_week_weekday?, member_quantities?}``.
    """

    @transaction.atomic
    def post(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        # Program-tab guard: a closed program is frozen -- no kitchen/cadence
        # (re)assignment, which would otherwise re-activate a closed member.
        if _program_locked(enr):
            return _program_locked_response()

        # CHANGING an already-assigned kitchen (the program tab's "Kitchen &
        # Delivery" Change control) is Management-only: verification / CS /
        # logistics agents can't alter a household's kitchen once set. The
        # INITIAL assignment (no kitchen yet -- the Logistics Kitchen Assignment
        # step) stays open to non-management logistics staff.
        if enr.kitchen_id and not is_management_group(
            getattr(getattr(request, "user", None), "group", None)
        ):
            return Response(
                {"error": "Only Management users can change the household's kitchen."},
                status=http.HTTP_403_FORBIDDEN,
            )

        kitchen_id = request.data.get("kitchen_id")
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None

        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        if kitchen is None:
            return Response(
                {"error": "kitchen_id is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if cadence not in active_cadence_codes():
            return Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence not in {c.code for c in kitchen.cadences.all()}:
            return Response(
                {"error": f"{kitchen.name} isn't configured for the selected cadence. Set the kitchen's cadences in Settings."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence_needs_weekday(cadence) and not once_weekday:
            return Response(
                {"error": "A delivery day is required for this cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # Per-member quantity overrides: {member_profile_id: qty}.
        raw_qty = request.data.get("member_quantities") or {}
        member_quantities = {}
        for key, val in raw_qty.items():
            try:
                member_quantities[int(key)] = int(val)
            except (TypeError, ValueError):
                continue

        # Members the agent manually excluded from THIS assignment (pulled Out of
        # Orbit), each with an optional customer-facing note. Applied AFTER the
        # meal rule so the override wins even when the member could otherwise be
        # served. Body: ``member_overrides: [{member_id, out_of_orbit, note?}]``.
        exclude_notes = {}
        for ov in request.data.get("member_overrides") or []:
            try:
                if ov.get("out_of_orbit"):
                    exclude_notes[int(ov.get("member_id"))] = (ov.get("note") or "").strip()
            except (TypeError, ValueError, AttributeError):
                continue

        summary = assign_kitchen_to_household(
            enr, client, kitchen, cadence=cadence, once_weekday=once_weekday,
            member_quantities=member_quantities, exclude_notes=exclude_notes,
            agent=current_agent(request),
        )
        resp = {
            "id": enr.pk,
            "stage": enr.stage,
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
        }
        # Surface which members changed status because of the new kitchen, so the
        # Household tab can show a banner (mirrors the address-change coverage
        # feedback): the ruleset (ZIP + menu/allergy fulfillment) is re-run for
        # every member against the newly assigned kitchen.
        out_names = summary.get("out_names") or []
        reactivated = summary.get("reactivated_names") or []
        if out_names:
            resp["coverage_warning"] = (
                f"{len(out_names)} member(s) set Out of Orbit — {kitchen.name} can't "
                f"fulfill their menu/allergies (or their delivery ZIP is excluded): "
                f"{', '.join(out_names)}."
            )
        if reactivated:
            resp["coverage_info"] = (
                f"{len(reactivated)} member(s) returned to Active — {kitchen.name} "
                f"can now fulfill them: {', '.join(reactivated)}."
            )
        return Response(resp)


def _enrollment_kind(enr):
    """Meals/Boxes kind for an enrollment. Uses the robust resolver so a program
    name without a 'meal'/'box' keyword still resolves via the linked
    ProductType or an existing delivery schedule."""
    return product_kind_for_enrollment(enr)


def _awaiting_enrollments(kind):
    """Enrollments awaiting kitchen assignment for a given product ``kind``
    (meals/boxes). Mirrors the Logistics queue (stage=kitchen_assignment)
    filtered to the kind, which is derived from the program name so meals/boxes
    never mix."""
    qs = (
        EnrollmentVerification.objects.filter(stage=EnrollmentStage.KITCHEN_ASSIGNMENT)
        .select_related("client", "case", "case__program")
    )
    return [e for e in qs if _enrollment_kind(e) == kind]


def _awaiting_box_enrollments():
    """Box households awaiting kitchen assignment (see :func:`_awaiting_enrollments`)."""
    return _awaiting_enrollments(ProductTypeKind.BOXES)


def _prefetched_kitchens():
    """Kitchens with their offered menus + restrictions prefetched, for reuse
    across serviceability checks in one request."""
    return list(
        Kitchen.objects.all().prefetch_related(
            "kitchen_menu_types__menu_type",
            "kitchen_menu_types__restrictions",
        )
    )


def enrollment_ready_for_assignment(enr, kitchens):
    """Whether a household enrollment is 'Ready to assign' -- the same readiness
    shown on the Logistics list: a delivery address is set, every member has a
    menu type and isn't predicted Out of Orbit, and some single kitchen can serve
    every member. Used by the bulk-assign 'only ready to assign' option."""
    if enr.delivery_address_id is None:
        return False
    members = list(enr.member_profiles.all())
    if not members:
        return False
    required = required_product_for_program(enr.program_name)
    serving_sets = []
    for mp in members:
        # Kitchen-AWARE (matches the Logistics list): missing menu, or NO
        # available kitchen able to serve the member's menu + allergies, means
        # not ready. The old kitchen-agnostic predict_member_out_of_orbit flagged
        # members a real kitchen could serve, wrongly excluding ready households.
        if not (mp.menu_type or "").strip():
            return False
        serving = {
            sk["kitchen"].pk
            for sk in serving_kitchens_for_member(
                mp, kitchens=kitchens, required_product=required,
            )
        }
        if not serving:
            return False
        serving_sets.append(serving)
    return bool(set.intersection(*serving_sets))


class BulkAssignBoxesView(PortalAPIView):
    """Logistics: bulk-assign the single box kitchen to every household awaiting
    kitchen assignment for a boxes program.

    GET returns the box-capable kitchens (for the agent to pick from) and the
    number of boxes households currently awaiting assignment. POST body
    ``{kitchen_id}`` runs the SAME kitchen-output rules + activation as the
    single-household assignment for each, one independent transaction per
    household so one failure never rolls back the rest.
    """

    def get(self, request):
        kitchens = [
            {
                "id": str(k.pk),
                "name": k.name,
                "status": k.status,
                "cadence_codes": [c.code for c in k.cadences.all() if c.is_active],
            }
            for k in Kitchen.objects.filter(
                supported_products__contains=[KitchenProductType.BOX]
            ).prefetch_related("cadences").order_by("name")
        ]
        box_enr = _awaiting_box_enrollments()
        kitchens_ctx = _prefetched_kitchens()
        return Response({
            "kitchens": kitchens,
            "cadence_options": cadence_options_for_kind(ProductTypeKind.BOXES),
            "awaiting_count": len(box_enr),
            "ready_count": sum(
                1 for e in box_enr if enrollment_ready_for_assignment(e, kitchens_ctx)
            ),
        })

    def post(self, request):
        kitchen_id = request.data.get("kitchen_id")
        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        if kitchen is None:
            return Response(
                {"error": "kitchen_id is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if KitchenProductType.BOX not in (kitchen.supported_products or []):
            return Response(
                {"error": f"{kitchen.name} does not make boxes."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # The cadence (and thus the delivery weekday) comes from the SELECTED
        # kitchen -- box kitchens can deliver on different days. Auto-use it when
        # the kitchen runs exactly one; otherwise the agent picks which.
        kitchen_cadences = [c.code for c in kitchen.cadences.all() if c.is_active]
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None
        if not cadence:
            if len(kitchen_cadences) == 1:
                cadence = kitchen_cadences[0]
            elif kitchen_cadences:
                return Response(
                    {"error": f"{kitchen.name} runs multiple cadences — select one."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            else:
                return Response(
                    {"error": f"{kitchen.name} has no cadence configured. Set its cadences in Settings."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
        if cadence not in active_cadence_codes():
            return Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence not in kitchen_cadences:
            return Response(
                {"error": f"{kitchen.name} isn't configured for the selected cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        agent = current_agent(request)
        enrollments = _awaiting_box_enrollments()
        # Optionally restrict to households that are Ready to assign (matches the
        # Logistics list's readiness): skip any with blockers.
        if request.data.get("ready_only"):
            kitchens_ctx = _prefetched_kitchens()
            enrollments = [
                e for e in enrollments
                if enrollment_ready_for_assignment(e, kitchens_ctx)
            ]

        assigned, out_of_orbit, failed, errors = 0, 0, 0, []
        for enr in enrollments:
            try:
                with transaction.atomic():
                    result = assign_kitchen_to_household(
                        enr, enr.client, kitchen, cadence=cadence,
                        once_weekday=once_weekday, agent=agent,
                    )
                assigned += 1
                out_of_orbit += result.get("out_of_orbit", 0)
            except Exception as exc:  # isolate a bad household; keep going
                failed += 1
                errors.append({
                    "client_id": str(enr.client_id) if enr.client_id else None,
                    "enrollment": enr.code,
                    "error": str(exc),
                })

        return Response({
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
            "total": len(enrollments),
            "assigned": assigned,
            "out_of_orbit": out_of_orbit,
            "failed": failed,
            "errors": errors,
        })


def _household_name(enr):
    """Display name for a household: the primary client's name, else client id."""
    c = enr.client
    if c is not None:
        name = f"{c.first_name or ''} {c.last_name or ''}".strip()
        if name:
            return name
    return str(enr.client_id) if enr.client_id else enr.code


_FOOD_ALLERGY_LABELS = dict(FoodAllergy.choices)


def _member_allergy_labels(profile):
    """Human-readable food-allergy labels for a member (drops the no-op 'none')."""
    return [
        _FOOD_ALLERGY_LABELS.get(c, c)
        for c in (profile.food_allergies or [])
        if c and c != "none"
    ]


def _preview_household_for_kitchen(enr, kitchen, offered):
    """Dry-run the kitchen-output rules for every member of ``enr`` against
    ``kitchen`` WITHOUT saving, so we can show the agent who would end up Out of
    Orbit before committing. Uses the exact same resolver the apply path runs.

    Returns ``{client_id, name, member_count, out_members, fully_covered}`` where
    each out member carries their menu type + allergies so the agent can see WHY.
    """
    out_members = []
    members = list(enr.member_profiles.select_related("client").all())
    for m in members:
        out, _became, reason = reconcile_member_kitchen_output(
            m, kitchen, offered=offered, save=False,
        )
        if out:
            out_members.append({
                "name": m.member_name or "Member",
                "reason": reason,
                "menu_type": m.menu_type or "",
                "allergies": _member_allergy_labels(m),
            })
    return {
        "client_id": str(enr.client_id) if enr.client_id else None,
        "name": _household_name(enr),
        "member_count": len(members),
        "out_members": out_members,
        "fully_covered": not out_members,
    }


class BulkAssignMealsView(PortalAPIView):
    """Logistics: preview + bulk-assign a meals kitchen to households awaiting
    kitchen assignment for a meals program.

    Unlike boxes, meals kitchens differ in menu/allergy coverage, so this is a
    review-first flow:

    * ``GET``  -> meal-capable kitchens, meals cadence options, awaiting count.
    * ``POST`` ``{kitchen_id, cadence, once_a_week_weekday?, preview: true}``
      -> a dry run (NO writes) reporting, per household, who would be set Out of
      Orbit by the chosen kitchen.
    * ``POST`` ``{kitchen_id, cadence, once_a_week_weekday?, only_covered}``
      -> applies. ``only_covered`` (default true) skips households the kitchen
      can't fully serve; set false to assign anyway (excluded members go Out of
      Orbit). Runs the SAME output rules + activation as the single assignment,
      one transaction per household.
    """

    def get(self, request):
        kitchens = [
            {
                "id": str(k.pk),
                "name": k.name,
                "status": k.status,
                "cadence_codes": [c.code for c in k.cadences.all() if c.is_active],
            }
            for k in Kitchen.objects.filter(
                supported_products__contains=[KitchenProductType.MEAL]
            ).prefetch_related("cadences").order_by("name")
        ]
        meal_enr = _awaiting_enrollments(ProductTypeKind.MEALS)
        kitchens_ctx = _prefetched_kitchens()
        return Response({
            "kitchens": kitchens,
            "cadence_options": cadence_options_for_kind(ProductTypeKind.MEALS),
            "awaiting_count": len(meal_enr),
            "ready_count": sum(
                1 for e in meal_enr if enrollment_ready_for_assignment(e, kitchens_ctx)
            ),
        })

    def _validated_inputs(self, request):
        """Shared kitchen + cadence validation. Returns (kitchen, cadence,
        once_weekday, error_response)."""
        kitchen_id = request.data.get("kitchen_id")
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None

        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        if kitchen is None:
            return None, None, None, Response(
                {"error": "kitchen_id is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if KitchenProductType.MEAL not in (kitchen.supported_products or []):
            return None, None, None, Response(
                {"error": f"{kitchen.name} does not make meals."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence not in active_cadence_codes():
            return None, None, None, Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence not in {c.code for c in kitchen.cadences.all()}:
            return None, None, None, Response(
                {"error": f"{kitchen.name} isn't configured for the selected cadence. Set the kitchen's cadences in Settings."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence_needs_weekday(cadence) and not once_weekday:
            return None, None, None, Response(
                {"error": "A delivery day is required for this cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        return kitchen, cadence, once_weekday, None

    def post(self, request):
        kitchen, cadence, once_weekday, err = self._validated_inputs(request)
        if err is not None:
            return err

        enrollments = _awaiting_enrollments(ProductTypeKind.MEALS)
        # Optionally restrict to households that are Ready to assign (matches the
        # Logistics list's readiness). Applies to both the preview and the apply.
        if request.data.get("ready_only"):
            kitchens_ctx = _prefetched_kitchens()
            enrollments = [
                e for e in enrollments
                if enrollment_ready_for_assignment(e, kitchens_ctx)
            ]
        offered = kitchen_offered_menu_index(kitchen)

        # Preview: dry-run only, report households that would have exclusions.
        if request.data.get("preview"):
            fully_covered, with_exclusions, households = 0, 0, []
            for enr in enrollments:
                prev = _preview_household_for_kitchen(enr, kitchen, offered)
                if prev["fully_covered"]:
                    fully_covered += 1
                else:
                    with_exclusions += 1
                    households.append(prev)
            return Response({
                "kitchen_id": str(kitchen.pk),
                "kitchen_name": kitchen.name,
                "total": len(enrollments),
                "fully_covered": fully_covered,
                "with_exclusions": with_exclusions,
                # Only the households needing attention (keeps the payload bounded).
                "households": households,
            })

        # Apply.
        only_covered = request.data.get("only_covered", True)
        agent = current_agent(request)
        assigned, skipped, out_of_orbit, failed, errors = 0, 0, 0, 0, []
        for enr in enrollments:
            if only_covered:
                prev = _preview_household_for_kitchen(enr, kitchen, offered)
                if not prev["fully_covered"]:
                    skipped += 1
                    continue
            try:
                with transaction.atomic():
                    result = assign_kitchen_to_household(
                        enr, enr.client, kitchen, cadence=cadence,
                        once_weekday=once_weekday, agent=agent,
                    )
                assigned += 1
                out_of_orbit += result.get("out_of_orbit", 0)
            except Exception as exc:  # isolate a bad household; keep going
                failed += 1
                errors.append({
                    "client_id": str(enr.client_id) if enr.client_id else None,
                    "enrollment": enr.code,
                    "error": str(exc),
                })

        return Response({
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
            "total": len(enrollments),
            "assigned": assigned,
            "skipped": skipped,
            "out_of_orbit": out_of_orbit,
            "failed": failed,
            "errors": errors,
        })


class MemberKitchenView(PortalAPIView):
    """Change the household's assigned kitchen from the member profile editor.

    The assignment is household-wide: it updates the enrollment and any existing
    delivery-plan snapshots. PATCH body: ``{kitchen_id}`` (null clears it).

    Locked to Management: changing the shared-household kitchen from the program
    tab's "Kitchen & Delivery" section is a high-impact action, so verification /
    CS / logistics agents are read-only here."""

    permission_classes = [IsManagementAgent]

    def patch(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        kitchen_id = request.data.get("kitchen_id")
        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        # Changing the kitchen must keep a valid cadence: the household's current
        # cadence has to be one the new kitchen runs, otherwise the delivery plan
        # would point at a cadence the kitchen doesn't fulfill. Ask the agent to
        # reassign via the cadence-first Kitchen Assignment popup instead.
        if kitchen is not None:
            current_cadence = current_household_cadence(enr)
            if current_cadence and current_cadence not in {
                c.code for c in kitchen.cadences.all()
            }:
                return Response(
                    {"error": f"{kitchen.name} doesn't run this household's current cadence. Reassign the kitchen from the Kitchen Assignment popup to pick a compatible cadence."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
        previous_kitchen = enr.kitchen.name if enr.kitchen_id else ""
        enr.kitchen = kitchen
        enr.save(update_fields=["kitchen"])
        enr.delivery_schedules.update(kitchen=kitchen)
        # Record the kitchen change (prev -> new) on the primary's history.
        try:
            timeline.event_for_kitchen_changed(
                enr, previous_kitchen=previous_kitchen,
                new_kitchen=kitchen.name if kitchen else "",
                actor=_agent_actor(current_agent(request)),
            )
        except Exception:  # never let history-logging break the change
            pass
        # Also refresh the already-generated future delivery occurrences so PO
        # generation groups this household under the NEW kitchen (the calendar
        # snapshots the kitchen at build time and is otherwise never rebuilt).
        resync_scheduled_orders(enrollment=enr)
        # Changing the kitchen changes the inputs to the kitchen warnings, so
        # reconcile the snapshot: a now-assigned kitchen clears a stale "No
        # kitchen assigned" row (and a compatible one clears the mismatch),
        # instead of lingering on Care Management. Best-effort.
        try:
            sync_household_warnings(enr)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "warning sync failed after kitchen change for enrollment %s", enr.pk
            )
        return Response({
            "kitchen_id": str(kitchen.pk) if kitchen else None,
            "kitchen_name": kitchen.name if kitchen else "",
        })


class MemberCadenceView(PortalAPIView):
    """Change the household's delivery cadence from the member profile editor.

    Household-wide: recomputes the delivery plan (weekdays, first delivery,
    per-delivery quantity, totals) on every existing schedule. Delivery weekdays
    come from the chosen cadence for both meals and boxes. PATCH body:
    ``{cadence, once_a_week_weekday?}``."""

    @transaction.atomic
    def patch(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None
        if cadence not in active_cadence_codes():
            return Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if enr.kitchen_id and cadence not in {c.code for c in enr.kitchen.cadences.all()}:
            return Response(
                {"error": f"{enr.kitchen.name} isn't configured for the selected cadence. Change the kitchen or update its cadences in Settings."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence_needs_weekday(cadence) and not once_weekday:
            return Response(
                {"error": "A delivery day is required for this cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        case = enr.case or s.primary_case(client)
        previous_cadence = current_household_cadence(enr) or ""
        update_household_cadence(
            enr, cadence=cadence, once_a_week_weekday=once_weekday, case=case
        )
        # Record the cadence change (prev -> new) on the primary's history,
        # rendered with the human labels. Best-effort.
        try:
            _cad_label = {c.code: c.label for c in Cadence.objects.all()}
            timeline.event_for_kitchen_changed(
                enr,
                previous_cadence=_cad_label.get(previous_cadence, previous_cadence),
                new_cadence=_cad_label.get(cadence, cadence),
                actor=_agent_actor(current_agent(request)),
            )
        except Exception:  # never let history-logging break the change
            pass
        # A cadence change moves the delivery DATES, so the existing dated
        # calendar must be rebuilt (not just field-resynced): drop future
        # occurrences no longer in the plan and add the new ones, leaving any
        # date already batched into a PO untouched.
        sync_delivery_calendar(enr)
        # A cadence change changes the inputs to the cadence warnings, so
        # reconcile the snapshot (clears a stale "No cadence assigned" or a
        # cadence/kitchen mismatch) rather than waiting for the nightly sweep.
        try:
            sync_household_warnings(enr)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "warning sync failed after cadence change for enrollment %s", enr.pk
            )
        row = Cadence.objects.filter(code=cadence).first()
        return Response({
            "cadence": current_household_cadence(enr) or cadence,
            "cadence_label": (row.label if row else "") or cadence,
        })


class MemberDiagnosticView(PortalAPIView):
    """GET: a read-only service-readiness diagnostic for a client.

    Returns a grouped checklist (coverage, case, lifecycle, verification,
    logistics, tickets) with per-check status (ok/warn/fail/na) plus an overall
    ``ready_for_service`` flag and the list of blocking checks. Never mutates
    state. See api.services.client_diagnostic.diagnose_client.
    """

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        return Response(diagnose_client(client))

"""Serializers for the support portal API.

Read serializers compose existing models into the shapes the React frontend
needs; UI fields with no backing model data are simply omitted (per plan).
Only two model additions back this layer: ``TicketNote`` and
``MenuType.is_active``.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from api.services.lifecycle import (
    governing_case_key,
    governing_pending_enrollment,
    verification_completed,
)

from ..models import (
    Address,
    Agent,
    Cadence,
    Case,
    CaseType,
    CommunicationChannel,
    CommunicationTimeOfDay,
    DeliveryCompany,
    DeliveryCompanyIntegration,
    DeliveryOrder,
    DietaryTag,
    EnrollmentVerification,
    Insurance,
    Kitchen,
    KitchenIntegration,
    KitchenMenuType,
    KitchenProductType,
    MemberDietaryProfile,
    MemberStatus,
    MenuType,
    Note,
    PurchaseOrder,
    ServiceAuthorizationStatus,
    SocialCareCoverage,
    StageEvent,
    Ticket,
    TicketNote,
    TicketSource,
    TicketType,
    TimelineEvent,
)

EXPIRING_WINDOW_DAYS = 30
LIFETIME_SENTINEL_YEAR = 9999


# ---------------------------------------------------------------------------
# Derived-field helpers
# ---------------------------------------------------------------------------
def _full_name(client):
    return f"{client.first_name} {client.last_name}".strip()


def _agent_name(agent):
    """Display name for an Agent (falls back to first+last, then agent_code).
    Returns None when there is no agent."""
    if agent is None:
        return None
    name = (agent.name or "").strip()
    if not name:
        name = f"{(agent.first_name or '').strip()} {(agent.last_name or '').strip()}".strip()
    return name or (agent.agent_code or None)


_COMM_CHANNEL_LABELS = dict(CommunicationChannel.choices)
_COMM_TIME_LABELS = dict(CommunicationTimeOfDay.choices)


def _labels_for(codes, mapping):
    """Map a stored list of enum codes to their human labels, keeping unknown
    codes as-is. Returns a clean list (blank/None entries dropped)."""
    if not codes:
        return []
    return [mapping.get(c, str(c)) for c in codes if c]


def primary_insurance(client):
    """The client's primary insurance (is_primary first, else most recent)."""
    plans = list(client.insurances.all())
    if not plans:
        return None
    for p in plans:
        if p.is_primary:
            return p
    return plans[0]


def medicaid_member_id(client):
    """Member id shown as 'Medicaid ID' = primary insurance external_member_id."""
    plans = list(client.insurances.all())
    medicaid = [p for p in plans if p.plan_type == "medicaid" and p.external_member_id]
    if medicaid:
        primary = next((p for p in medicaid if p.is_primary), medicaid[0])
        return primary.external_member_id
    ins = primary_insurance(client)
    return ins.external_member_id if ins and ins.external_member_id else ""


def member_flags(client):
    """UI flag chips derived from existing data: Veteran / Family / Level / Dual."""
    flags = []
    mp = getattr(client, "military_profile", None)
    if mp and mp.military_affiliation in ("veteran", "service_member"):
        flags.append("Veteran")
    if client.is_a_family:
        flags.append("Family")
    if client.is_level:
        flags.append(client.get_is_level_display())
    plan_types = {p.plan_type for p in client.insurances.all() if p.status == "active"}
    if {"medicaid", "medicare"} <= plan_types:
        flags.append("Dual")
    return flags


# lifecycle_stage -> coarse verification status used by the members filter.
# Verification is a yes/no fact (Pending Verification / Verified). The case
# authorization outcome is a SEPARATE dimension (see authorization_status) and
# never appears here.
_STATUS_MAP = {
    "not_eligible": "Denied",
    "pending_verification": "Pending Verification",
    "verified": "Verified",
    "kitchen_assignment": "Kitchen Assignment",
    "active": "Active",
    "completed": "Completed",
}


def verification_status(client):
    # On Hold is a service-state overlay on top of the member's real stage: a
    # held member keeps their underlying stage (e.g. Active) but the list shows
    # "On Hold" until service resumes.
    if service_hold_state(client)["on_hold"]:
        return "On Hold"
    # Verification is a yes/no fact: until the pop-up is completed the member is
    # Pending Verification, regardless of any case authorization status (which is
    # surfaced separately via authorization_status). This guards against a stage
    # that implies verification without the pop-up actually having been done.
    if not verification_completed(client) and client.lifecycle_stage in (
        "pending_verification",
        "verified",
        "kitchen_assignment",
    ):
        return "Pending Verification"
    return _STATUS_MAP.get(client.lifecycle_stage, client.get_lifecycle_stage_display())


def verification_pending(client):
    """True when the member has a PENDING verification request -- the only state
    in which the "run verification" pop-up is offered. Requests originate from
    the ext (the CRM never initiates one), so the button appears only to
    complete or disregard an existing request.

    Pending means either a governing enrollment sits at ``pending_verification``,
    or (for imported members with no enrollment row) the client's lifecycle_stage
    is pending_verification. Everything else -- navigation, verified, disregarded,
    cancelled -- has no live request, so the button hides.
    """
    if governing_pending_enrollment(client) is not None:
        return True
    return client.lifecycle_stage == "pending_verification"


# Coarse pipeline PHASE for the Verification list's "Stage" column. Both
# pending_verification and verified (awaiting authorization) are still in the
# "Verification" phase; the case being approved advances to Kitchen Assignment,
# then Active once the kitchen is assigned. On Hold overlays any phase.
_STAGE_PHASE_LABELS = {
    "pending_verification": "Verification",
    "verified": "Verification",
    "kitchen_assignment": "Kitchen Assignment",
    "active": "Active",
    "completed": "Completed",
    "not_eligible": "Denied",
}


def pipeline_stage_label(client):
    """The member's CURRENT pipeline phase, independent of the verification fact:
    the On Hold overlay if paused, otherwise the coarse phase for the client's
    lifecycle_stage. The whole verification window (pending OR verified-awaiting-
    authorization) reads as "Verification"; approval moves it to Kitchen
    Assignment, then Active. Used by the Verification list's "Stage" column."""
    # Cancelled is a terminal off-ramp that collapses lifecycle_stage to
    # not_eligible; surface it explicitly (like the On Hold overlay) so the list
    # Stage column reads "Cancelled" instead of "Not Eligible".
    if service_cancelled_state(client)["cancelled"]:
        return "Cancelled"
    if service_hold_state(client)["on_hold"]:
        return "On Hold"
    return _STAGE_PHASE_LABELS.get(
        client.lifecycle_stage, client.get_lifecycle_stage_display()
    )


def authorization_status(client):
    """The meal/box case authorization that gates kitchen assignment, as a
    ``{status, status_label, is_accepted}`` snapshot. Sourced ONLY from the
    client's Internal Service case -- members without one have no authorization
    to show (empty values). This is a separate dimension from
    verification_status, surfaced as its own column."""
    return case_authorization(internal_service_case(client))


def active_enrollment(client):
    """Most recent non-closed enrollment governing the client (drives status /
    household / dates).

    The verification applies to the WHOLE household, so a non-primary member has
    no enrollment of their own: fall back to their household's enrollment. Without
    this, only the primary reflects the household's denied / on-hold / date state
    while every other member falls back to the coarser ``lifecycle_stage`` (e.g.
    showing "Waiting Authorization" instead of "Authorization Denied").
    """
    # DISREGARDED enrollments are dismissed verification requests kept only for
    # history -- never treat them as the client's active enrollment.
    enrollments = [
        e for e in client.enrollments.all() if e.stage != "disregarded"
    ]
    if not enrollments:
        membership = getattr(client, "household_membership", None)
        if membership is not None:
            enrollments = [
                e
                for e in membership.household.enrollment_verifications.all()
                if e.stage != "disregarded"
            ]
    if not enrollments:
        return None
    open_ones = [e for e in enrollments if e.closed_at is None]
    pool = open_ones or enrollments
    return sorted(pool, key=lambda e: e.opened_at or timezone.now(), reverse=True)[0]


def active_member_profile(client):
    """The client's dietary profile for their ACTIVE enrollment (drives the
    per-member Out of Orbit / Paused sub-status). None when there's no profile
    on the active enrollment, so a stale profile from a closed enrollment never
    mislabels a member."""
    profiles = list(client.member_profiles.all())
    if not profiles:
        return None
    enr = active_enrollment(client)
    if enr is None:
        return None
    for mp in profiles:
        if mp.enrollment_id == enr.pk:
            return mp
    return None


def member_out_of_orbit(client):
    """True when the client's active-enrollment dietary profile is Out of Orbit
    (the meal rule couldn't be safely fulfilled). Out-of-orbit members are
    excluded from delivery schedules/POs, so the Logistics roster hides them."""
    mp = active_member_profile(client)
    return mp is not None and mp.status == MemberStatus.OUT_OF_ORBIT


def member_out_of_range(client):
    """True when the client's active-enrollment dietary profile is Out of Range
    (delivery/primary ZIP outside the coverage area). Out-of-range members are
    excluded from delivery schedules/POs, so the Logistics roster hides them."""
    mp = active_member_profile(client)
    return mp is not None and mp.status == MemberStatus.OUT_OF_RANGE


def member_paused(client):
    """True when the client's active-enrollment dietary profile is Paused (an
    agent manually paused them). Paused members are excluded from delivery
    schedules/POs."""
    mp = active_member_profile(client)
    return mp is not None and mp.status == MemberStatus.PAUSED


def service_hold_state(client):
    """Whether the client's household service is paused (enrollment On Hold).

    Drives the Hold/Resume button in the member header. ``can_hold`` is true
    when there is an active enrollment that is not already on hold.
    """
    enr = active_enrollment(client)
    on_hold = bool(enr and enr.stage == "on_hold")
    return {
        "has_enrollment": enr is not None,
        "on_hold": on_hold,
        # Hold is only offered for a live, non-terminal enrollment -- never for a
        # household that is already on hold, cancelled, closed or completed.
        "can_hold": bool(
            enr is not None
            and enr.stage not in ("on_hold", "cancelled", "closed", "service_complete")
        ),
        "enrollment_stage": enr.stage if enr else None,
    }


def service_cancelled_state(client):
    """Whether the client's household enrollment was CANCELLED (a terminal hard
    off-ramp). Drives the "Household Cancelled" pill + the terminal Cancelled
    node on the stages progress bar (a cancellation collapses lifecycle_stage to
    not_eligible, which alone can't be told apart from an eligibility denial, so
    the UI keys off this instead).

    ``can_cancel`` is true when there is an active enrollment that isn't already
    terminal (cancelled / closed) or completed.
    """
    enr = active_enrollment(client)
    cancelled = bool(enr and enr.stage == "cancelled")
    cancelled_from = None
    if cancelled:
        # The enrollment stage the household was cancelled FROM, so the progress
        # bar can still show how far they got (stages 1-5) before the terminal
        # Cancelled node -- read off the most recent transition into Cancelled.
        ev = (
            StageEvent.objects.filter(enrollment=enr, to_stage="cancelled")
            .order_by("-entered_at")
            .first()
        )
        cancelled_from = ev.from_stage if ev else None
    return {
        "cancelled": cancelled,
        "cancelled_at": (enr.stage_at.isoformat() if enr and enr.stage_at else None)
        if cancelled
        else None,
        "cancelled_from": cancelled_from,
        "can_cancel": bool(
            enr is not None
            and enr.stage not in ("cancelled", "closed", "service_complete")
        ),
    }


def primary_case(client):
    """The case whose service authorization governs the client: the most
    favorable / most recently opened case that carries an authorization status,
    else the most recent case. Chosen by :func:`governing_case_key` so an
    approved authorization wins over a same-or-earlier-dated denial. None when
    the client has no cases."""
    cases = list(client.cases.all())
    if not cases:
        return None
    with_auth = [c for c in cases if c.service_authorization_status]
    pool = with_auth or cases
    return max(pool, key=governing_case_key)


def internal_service_case(client):
    """The client's Internal Service case — the one the verification and meal/box
    delivery attach to. Chosen by :func:`governing_case_key` (authorization
    favorability first, then recency), so an approved case wins over a
    same-or-earlier-dated denial. None when there is no Internal Service case."""
    cases = [
        c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE
    ]
    if not cases:
        return None
    return max(cases, key=governing_case_key)


def internal_service_cases(client):
    """All of the client's Internal Service cases, most-governing first (by
    :func:`governing_case_key`). The verification can attach to any of them; the
    agent picks which one in the verification pop-up when there's more than one."""
    cases = [
        c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE
    ]
    return sorted(cases, key=governing_case_key, reverse=True)


def case_authorization(case):
    """Read-only authorization snapshot for a case: normalized status, the raw
    display label (e.g. "Accepted"), and whether it counts as accepted."""
    if case is None:
        return {"status": "", "status_label": "", "is_accepted": False}
    status = case.service_authorization_status or ""
    label = case.service_authorization_status_label or (
        case.get_service_authorization_status_display() if status else ""
    )
    return {
        "status": status,
        "status_label": label,
        "is_accepted": status == ServiceAuthorizationStatus.APPROVED,
    }


def is_insurance_expiring(plan):
    if plan.status != "active" or not plan.expired_at:
        return False
    return plan.expired_at <= timezone.now() + timedelta(days=EXPIRING_WINDOW_DAYS)


def _fmt_end(dt):
    """Format an expiry datetime, mapping the lifetime sentinel to a label."""
    if not dt:
        return None
    if dt.year >= LIFETIME_SENTINEL_YEAR:
        return "Lifetime"
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Members list + detail
# ---------------------------------------------------------------------------
class MemberListSerializer(serializers.Serializer):
    id = serializers.UUIDField(source="client_id")
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    name = serializers.SerializerMethodField()
    date_of_birth = serializers.DateField()
    lifecycle_stage = serializers.CharField()
    lifecycle_stage_label = serializers.CharField(source="get_lifecycle_stage_display")
    verification_status = serializers.SerializerMethodField()
    authorization_status = serializers.SerializerMethodField()
    authorization_status_label = serializers.SerializerMethodField()
    medicaid_id = serializers.SerializerMethodField()
    case_manager = serializers.CharField(source="agent_name")
    flags = serializers.SerializerMethodField()
    out_of_orbit = serializers.SerializerMethodField()
    out_of_range = serializers.SerializerMethodField()
    paused = serializers.SerializerMethodField()
    start_date = serializers.SerializerMethodField()
    end_date = serializers.SerializerMethodField()
    verification_requested_at = serializers.SerializerMethodField()
    verification_completed_at = serializers.SerializerMethodField()
    verification_requested_by = serializers.SerializerMethodField()
    verification_completed_by = serializers.SerializerMethodField()
    stage_label = serializers.SerializerMethodField()
    verification_state = serializers.SerializerMethodField()
    household_primary_id = serializers.SerializerMethodField()
    last_updated = serializers.DateTimeField(source="updated_at")
    created_at = serializers.DateTimeField()
    authorization_status_at = serializers.SerializerMethodField()
    stage_at = serializers.DateTimeField(source="lifecycle_stage_at")
    on_hold_at = serializers.SerializerMethodField()
    cancelled_at = serializers.SerializerMethodField()
    out_of_orbit_at = serializers.SerializerMethodField()
    out_of_range_at = serializers.SerializerMethodField()
    paused_at = serializers.SerializerMethodField()

    def get_name(self, obj):
        return _full_name(obj)

    def get_household_primary_id(self, obj):
        # client_id of the household's PRIMARY member, used by the Members list
        # to open the household head in the CRM. Returned for every member of a
        # household (including the primary itself, which links to itself); None
        # when the client has no household.
        membership = getattr(obj, "household_membership", None)
        if membership is None:
            return None
        for m in membership.household.members.all():
            if m.is_primary:
                return str(m.client_id)
        return None

    def get_stage_label(self, obj):
        return pipeline_stage_label(obj)

    def get_verification_state(self, obj):
        # Verification FACT column. Verified once the pop-up completed
        # (verified_at set). "Pending Verification" applies ONLY to members
        # actually in the verification window (a governing enrollment exists);
        # a bare client with no case/screening/enrollment is NOT pending -- it
        # has simply never entered verification, so surface its real lifecycle
        # stage instead (e.g. Inactive / Screened). Without this gate the
        # Members page mislabels every non-verified client as Pending
        # Verification. The downstream pipeline position lives in stage_label.
        if verification_completed(obj):
            return "Verified"
        if obj.lifecycle_stage in (
            "pending_verification",
            "verified",
            "kitchen_assignment",
        ):
            return "Pending Verification"
        # Not in the verification window (Inactive / Consent / Screened / etc.):
        # leave the Verification Status blank rather than echoing the lifecycle
        # stage -- these members have simply never entered verification.
        return ""

    def get_out_of_orbit(self, obj):
        return member_out_of_orbit(obj)

    def get_out_of_range(self, obj):
        return member_out_of_range(obj)

    def get_paused(self, obj):
        return member_paused(obj)

    def get_verification_status(self, obj):
        return verification_status(obj)

    def get_authorization_status(self, obj):
        return authorization_status(obj)["status"]

    def get_authorization_status_label(self, obj):
        return authorization_status(obj)["status_label"]

    def get_authorization_status_at(self, obj):
        # No dedicated "status decided at" field exists on the Case; the source's
        # last-update time (Case.updated_at) is the closest proxy for when we got
        # the current authorization status. Sourced from the internal-service case.
        case = internal_service_case(obj)
        return case.updated_at.isoformat() if case and case.updated_at else None

    def get_on_hold_at(self, obj):
        # Precise moment the household enrollment entered On Hold (enrollment
        # stage_at), or null when not currently on hold.
        if not service_hold_state(obj)["on_hold"]:
            return None
        enr = active_enrollment(obj)
        return enr.stage_at.isoformat() if enr and enr.stage_at else None

    def get_cancelled_at(self, obj):
        # Precise moment the enrollment was cancelled (already computed by
        # service_cancelled_state from stage_at); null when not cancelled.
        return service_cancelled_state(obj)["cancelled_at"]

    def get_out_of_orbit_at(self, obj):
        # When the member's active-enrollment profile last flipped status; only
        # meaningful while currently Out of Orbit.
        mp = active_member_profile(obj)
        if mp is None or mp.status != MemberStatus.OUT_OF_ORBIT:
            return None
        return mp.status_changed_at.isoformat() if mp.status_changed_at else None

    def get_out_of_range_at(self, obj):
        # When the member's active-enrollment profile last flipped status; only
        # meaningful while currently Out of Range.
        mp = active_member_profile(obj)
        if mp is None or mp.status != MemberStatus.OUT_OF_RANGE:
            return None
        return mp.status_changed_at.isoformat() if mp.status_changed_at else None

    def get_paused_at(self, obj):
        mp = active_member_profile(obj)
        if mp is None or mp.status != MemberStatus.PAUSED:
            return None
        return mp.status_changed_at.isoformat() if mp.status_changed_at else None

    def get_medicaid_id(self, obj):
        return medicaid_member_id(obj)

    def get_flags(self, obj):
        return member_flags(obj)

    def get_start_date(self, obj):
        enr = active_enrollment(obj)
        return enr.opened_at.isoformat() if enr and enr.opened_at else None

    def get_end_date(self, obj):
        enr = active_enrollment(obj)
        return enr.closed_at.isoformat() if enr and enr.closed_at else None

    def get_verification_requested_at(self, obj):
        # A verification is "requested" when its enrollment is created (it opens
        # at Pending Verification and fires the VERIFICATION_REQUESTED timeline).
        # A renewed/re-requested enrollment stamps requested_at, so prefer it and
        # fall back to opened_at (creation) when it was never re-requested.
        enr = active_enrollment(obj)
        if not enr:
            return None
        when = enr.requested_at or enr.opened_at
        return when.isoformat() if when else None

    def get_verification_completed_at(self, obj):
        # A verification is "done" when the pop-up completes and sets verified_at
        # (the source of truth for "is this household verified?"). Null while the
        # household is still Pending Verification.
        enr = active_enrollment(obj)
        return enr.verified_at.isoformat() if enr and enr.verified_at else None

    def get_verification_requested_by(self, obj):
        # Agent who submitted the E-Form that requested the verification. NULL for
        # bulk-imported enrollments with no attributable agent.
        enr = active_enrollment(obj)
        return _agent_name(enr.requested_by) if enr else None

    def get_verification_completed_by(self, obj):
        # Agent who completed the verification pop-up (set alongside verified_at).
        enr = active_enrollment(obj)
        return _agent_name(enr.verified_by) if enr else None


class MemberDetailSerializer(serializers.Serializer):
    """Composed member profile: core / lifecycle / demographics / contact /
    address / flags / care_team / alerts. SSN intentionally omitted."""

    def to_representation(self, client):
        ins = primary_insurance(client)
        svc_case = internal_service_case(client)
        current_addr = next(
            (a for a in client.addresses.all() if a.type == "current"),
            next(iter(client.addresses.all()), None),
        )
        return {
            "core": {
                "id": str(client.client_id),
                "first_name": client.first_name,
                "last_name": client.last_name,
                "name": _full_name(client),
                "date_of_birth": client.date_of_birth.isoformat()
                if client.date_of_birth
                else None,
                "gender": client.gender,
                "medicaid_id": medicaid_member_id(client),
                "verification_status": verification_status(client),
                # True once the verification pop-up has been COMPLETED (verified_at
                # set) -- stays true through the post-verification service stages.
                # Drives the header's "run verification" button, which is offered
                # only while the household is still pre-verification.
                "verification_completed": verification_completed(client),
                # True only while a PENDING verification request exists -- the
                # sole state in which the "run verification" pop-up is offered.
                "verification_pending": verification_pending(client),
                # Williamsburg exception (lead source == "Williamsburg"): the
                # verification wizard forces the Kosher menu and the save
                # auto-assigns the Williamsburg kitchen + activates directly.
                "is_williamsburg": bool(getattr(client, "is_williamsburg", False)),
                "lead_source": client.lead_source or "",
            },
            "lifecycle": {
                "stage": client.lifecycle_stage,
                "stage_label": client.get_lifecycle_stage_display(),
                "stage_at": client.lifecycle_stage_at.isoformat()
                if client.lifecycle_stage_at
                else None,
                "service_hold": service_hold_state(client),
                "service_cancelled": service_cancelled_state(client),
            },
            # Read-only authorization status sourced from the client's GOVERNING
            # internal-service case (the meal/box case that gates kitchen
            # assignment) -- falls back to the governing case of any type. Shown
            # as the profile's Authorization badge and in the verification
            # wizard's validation step. Separate dimension from verification.
            "authorization": case_authorization(svc_case or primary_case(client)),
            # The Internal Service case the verification + delivery attach to.
            # Its program name is shown (read-only) in the verification wizard.
            "service": {
                "program_name": svc_case.program_name if svc_case else "",
                "service_type": svc_case.service_type if svc_case else "",
                "case_id": str(svc_case.case_id) if svc_case else None,
                # Every Internal Service case the verification can attach to, so
                # the pop-up can let the agent switch the governing case when the
                # client holds more than one. `governing` marks the default pick.
                # DENIED-authorization cases are excluded: a denied meal/box case
                # can't be verified against, so it must not be an option in the
                # pop-up's case dropdown (any other status is still selectable).
                "cases": [
                    {
                        "case_id": str(c.case_id),
                        "program_name": c.program_name or "",
                        "service_type": c.service_type or "",
                        "authorization": case_authorization(c),
                        "governing": bool(svc_case and c.case_id == svc_case.case_id),
                    }
                    for c in internal_service_cases(client)
                    if c.service_authorization_status != ServiceAuthorizationStatus.DENIED
                ],
            },
            "demographics": {
                "gender": client.gender,
                "marital_status": client.marital_status,
                "race": client.race,
                "ethnicity": client.ethnicity,
                "language": client.language,
                "preferred_spoken_language": client.preferred_spoken_language,
                "preferred_written_language": client.preferred_written_language,
                "household_size": client.household_size,
            },
            "contact": {
                "phone": client.client_phone_number,
                "phone_type": client.phone_type,
                "email": client.client_email_address,
                "preferred_contact_method": client.preferred_contact_method,
                # Multi-select communication preferences captured by the ext.
                # Codes are mapped to human labels; raw codes kept for any UI
                # that needs them.
                "communication_channels": _labels_for(
                    client.communication_channels, _COMM_CHANNEL_LABELS
                ),
                "preferred_communication_times": _labels_for(
                    client.preferred_communication_times, _COMM_TIME_LABELS
                ),
                "preferred_languages": list(client.preferred_languages or []),
            },
            "address": {
                "street": current_addr.street if current_addr else "",
                "unit": current_addr.unit if current_addr else "",
                "city": current_addr.city if current_addr else "",
                "state": current_addr.state if current_addr else "",
                "zip": current_addr.zip if current_addr else "",
                "notes": current_addr.notes if current_addr else "",
            }
            if current_addr
            else None,
            "flags": member_flags(client),
            "care_team": {
                "case_manager": client.agent_name,
                "doctor_name": client.doctor_name,
            },
            "alerts": self._alerts(client, ins),
        }

    def _alerts(self, client, ins):
        alerts = []
        open_tickets = [t for t in client.tickets.all() if t.status != "resolved"]
        for t in open_tickets:
            alerts.append(
                {
                    "kind": "ticket",
                    "severity": t.severity,
                    "label": t.type.label,
                    "detail": t.reason,
                }
            )
        for plan in client.insurances.all():
            if is_insurance_expiring(plan):
                alerts.append(
                    {
                        "kind": "insurance_expiring",
                        "severity": "medium",
                        "label": f"{plan.plan_name or plan.get_plan_type_display()} expiring",
                        "detail": _fmt_end(plan.expired_at),
                    }
                )
        return alerts


# ---------------------------------------------------------------------------
# Insurance + social coverage
# ---------------------------------------------------------------------------
class PortalInsuranceSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="pk", read_only=True)
    plan_type_label = serializers.CharField(source="get_plan_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    is_expiring = serializers.SerializerMethodField()
    start_date = serializers.DateTimeField(source="enrolled_at", read_only=True)
    end_date = serializers.SerializerMethodField()

    class Meta:
        model = Insurance
        fields = [
            "id", "plan_type", "plan_type_label", "plan_name", "external_member_id",
            "external_group_id", "status", "status_label", "is_primary", "verified",
            "start_date", "end_date", "is_expiring",
        ]

    def get_is_expiring(self, obj):
        return is_insurance_expiring(obj)

    def get_end_date(self, obj):
        return _fmt_end(obj.expired_at)


class PortalSocialCoverageSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="pk", read_only=True)
    plan_type_label = serializers.CharField(source="get_plan_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    start_date = serializers.DateTimeField(source="enrolled_at", read_only=True)
    end_date = serializers.SerializerMethodField()

    class Meta:
        model = SocialCareCoverage
        fields = [
            "id", "plan_type", "plan_type_label", "plan_name", "external_member_id",
            "external_group_id", "status", "status_label", "verified",
            "start_date", "end_date",
        ]

    def get_end_date(self, obj):
        return _fmt_end(obj.expired_at)


# ---------------------------------------------------------------------------
# History (timeline)
# ---------------------------------------------------------------------------
def resolve_actor_name(actor, agent_names=None):
    """Human-readable name for a timeline event's ``actor`` attribution string.

    Actors are stored as ``agent:<agent_code>`` (portal/extension writes),
    ``system:<job>`` (batch jobs), ``user:<name>`` or a raw display name. This
    resolves an agent code to the Agent's name (via the optional prefetched
    ``agent_names`` {code: name} map, else a direct lookup) so the history tab
    can show WHO performed the action instead of an opaque code."""
    if not actor:
        return ""
    if actor.startswith("agent:"):
        code = actor[len("agent:"):]
        if agent_names is not None:
            return agent_names.get(code) or f"Agent {code}"
        name = Agent.objects.filter(agent_code=code).values_list("name", flat=True).first()
        return name or f"Agent {code}"
    if actor.startswith("system:"):
        return "System"
    if actor.startswith("user:"):
        return actor[len("user:"):]
    return actor


def build_actor_name_map(events):
    """Prefetch a {agent_code: name} map for the agent actors in ``events`` so a
    page of timeline rows resolves actor names in one query (no N+1)."""
    codes = {
        e.actor[len("agent:"):]
        for e in events
        if e.actor and e.actor.startswith("agent:")
    }
    if not codes:
        return {}
    return dict(
        Agent.objects.filter(agent_code__in=codes).values_list("agent_code", "name")
    )


class HistoryEventSummarySerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="pk", read_only=True)
    event_type_label = serializers.CharField(source="get_event_type_display", read_only=True)
    # Provenance (who/where) + a stable deep-link to the source entity so the UI
    # can badge "via Import #73 by Jane" and click through to the case/ticket.
    entity_type = serializers.SerializerMethodField()
    entity_id = serializers.CharField(source="object_id", read_only=True)
    # Resolved, human-readable actor (agent code -> agent name).
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = TimelineEvent
        fields = [
            "id", "event_type", "event_type_label", "occurred_at", "title",
            "subtitle", "badge_text", "badge_tone", "renewal_number",
            "source", "actor", "actor_name", "case", "entity_type", "entity_id",
            "metadata",
        ]

    def get_entity_type(self, obj):
        return obj.content_type.model if obj.content_type_id else None

    def get_actor_name(self, obj):
        return resolve_actor_name(obj.actor, self.context.get("actor_names"))


class HistoryEventDetailSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="pk", read_only=True)
    event_type_label = serializers.CharField(source="get_event_type_display", read_only=True)
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = TimelineEvent
        fields = [
            "id", "event_type", "event_type_label", "occurred_at", "title",
            "subtitle", "badge_text", "badge_tone", "renewal_number",
            "source", "actor", "actor_name", "metadata",
        ]

    def get_actor_name(self, obj):
        return resolve_actor_name(obj.actor, self.context.get("actor_names"))


class ActivityEventSerializer(HistoryEventSummarySerializer):
    """A timeline event for the cross-client admin Activity Log -- adds the
    client identity so each row links to the member profile."""

    client_id = serializers.CharField(source="client.client_id", read_only=True)
    client_name = serializers.SerializerMethodField()

    class Meta(HistoryEventSummarySerializer.Meta):
        fields = HistoryEventSummarySerializer.Meta.fields + [
            "client_id", "client_name",
        ]

    def get_client_name(self, obj):
        c = obj.client
        if c is None:
            return ""
        return f"{c.first_name} {c.last_name}".strip()


# ---------------------------------------------------------------------------
# Notes (Unite Us client/case notes)
# ---------------------------------------------------------------------------
class PortalNoteSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="pk", read_only=True)
    scope = serializers.SerializerMethodField()
    created = serializers.SerializerMethodField()

    class Meta:
        model = Note
        fields = ["id", "scope", "source", "author_name", "body", "created"]

    def get_scope(self, obj):
        return "case" if obj.case_id else "client"

    def get_created(self, obj):
        dt = obj.source_created_at or obj.created_at
        return dt.isoformat() if dt else None


class PortalNoteCreateSerializer(serializers.Serializer):
    body = serializers.CharField()
    case_id = serializers.UUIDField(required=False, allow_null=True)


# ---------------------------------------------------------------------------
# Tickets + ticket notes
# ---------------------------------------------------------------------------
def ticket_code(ticket):
    return f"TKT-{ticket.pk:04d}"


class PortalTicketNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketNote
        fields = ["id", "author_name", "body", "created_at"]


class PortalTicketSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="pk", read_only=True)
    code = serializers.SerializerMethodField()
    type = serializers.SlugRelatedField(slug_field="code", read_only=True)
    type_label = serializers.CharField(source="type.label", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    client_id = serializers.SerializerMethodField()
    client_name = serializers.SerializerMethodField()
    case_code = serializers.SerializerMethodField()
    assignee = serializers.SerializerMethodField()
    assignee_id = serializers.SerializerMethodField()
    origin = serializers.CharField(read_only=True)
    notes = PortalTicketNoteSerializer(many=True, read_only=True)

    class Meta:
        model = Ticket
        fields = [
            "id", "code", "type", "type_label", "status", "status_label",
            "severity", "source", "source_label", "reason", "client_id",
            "client_name", "case_code", "assignee", "assignee_id", "origin",
            "created_at", "updated_at", "resolved_at", "notes",
        ]

    def get_code(self, obj):
        return ticket_code(obj)

    def get_client_id(self, obj):
        return str(obj.client_id) if obj.client_id else None

    def get_client_name(self, obj):
        return _full_name(obj.client) if obj.client else ""

    def get_case_code(self, obj):
        if not obj.case_id:
            return None
        # Case has no short code; surface a stable display code from the UUID.
        return f"CSE-{str(obj.case_id)[:8]}"

    def get_assignee(self, obj):
        return obj.assigned_to.name if obj.assigned_to else None

    def get_assignee_id(self, obj):
        return str(obj.assigned_to_id) if obj.assigned_to_id else None


class PortalTicketTypeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="ticket_type_id", read_only=True)

    class Meta:
        model = TicketType
        fields = ["id", "code", "label", "description", "default_severity"]


class PortalCaseOptionSerializer(serializers.ModelSerializer):
    """Lightweight case row for the New-Ticket “related case” dropdown."""

    id = serializers.UUIDField(source="case_id", read_only=True)
    code = serializers.SerializerMethodField()
    status = serializers.CharField(source="case_status", read_only=True)
    status_label = serializers.CharField(source="get_case_status_display", read_only=True)
    type_label = serializers.CharField(source="get_case_type_display", read_only=True)
    date_opened = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Case
        fields = [
            "id", "code", "status", "status_label", "type_label",
            "service_type", "program_name", "date_opened",
        ]

    def get_code(self, obj):
        return f"CSE-{str(obj.case_id)[:8]}"


class PortalMemberCaseSerializer(serializers.ModelSerializer):
    """Full case row for the member profile's Cases tab."""

    id = serializers.UUIDField(source="case_id", read_only=True)
    code = serializers.SerializerMethodField()
    status = serializers.CharField(source="case_status", read_only=True)
    status_label = serializers.CharField(source="get_case_status_display", read_only=True)
    type = serializers.CharField(source="case_type", read_only=True)
    type_label = serializers.CharField(source="get_case_type_display", read_only=True)
    auth_status = serializers.CharField(
        source="service_authorization_status", read_only=True
    )
    auth_status_label = serializers.SerializerMethodField()
    provider_name = serializers.SerializerMethodField()
    resolution_type = serializers.CharField(
        source="outcome_resolution_type", read_only=True
    )
    resolution_label = serializers.CharField(
        source="get_outcome_resolution_type_display", read_only=True
    )

    class Meta:
        model = Case
        fields = [
            "id", "code", "status", "status_label", "type", "type_label",
            "service_type", "program_name", "provider_name",
            "primary_worker_name", "date_opened", "case_closed_at",
            "auth_status", "auth_status_label",
            "authorized_amount", "authorized_unit",
            "service_authorization_requested_amount",
            "service_authorization_approval_starts_at",
            "service_authorization_approval_ends_at",
            "service_authorization_request_starts_at",
            "service_authorization_request_ends_at",
            "outcome_description", "resolution_type", "resolution_label",
            "case_description",
        ]

    def get_code(self, obj):
        return f"CSE-{str(obj.case_id)[:8]}"

    def get_auth_status_label(self, obj):
        return obj.service_authorization_status_label or (
            obj.get_service_authorization_status_display()
            if obj.service_authorization_status
            else ""
        )

    def get_provider_name(self, obj):
        return obj.provider_name or obj.originating_provider_name or ""


class PortalTicketCreateSerializer(serializers.Serializer):
    type = serializers.SlugRelatedField(
        slug_field="code", queryset=TicketType.objects.all()
    )
    severity = serializers.ChoiceField(
        choices=["low", "medium", "high"], default="medium"
    )
    source = serializers.ChoiceField(
        choices=TicketSource.values, required=False, allow_blank=True, default=""
    )
    reason = serializers.CharField()
    client_id = serializers.UUIDField(required=False, allow_null=True)
    case_id = serializers.UUIDField(required=False, allow_null=True)
    assignee_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        case_id = attrs.get("case_id")
        if case_id:
            case = Case.objects.filter(pk=case_id).first()
            if case is None:
                raise serializers.ValidationError({"case_id": "Unknown case."})
            client_id = attrs.get("client_id")
            if client_id and str(case.client_id) != str(client_id):
                raise serializers.ValidationError(
                    {"case_id": "Case does not belong to the selected member."}
                )
        return attrs


# ---------------------------------------------------------------------------
# Agents (assignee dropdown)
# ---------------------------------------------------------------------------
class PortalAgentSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    group = serializers.CharField()
    email = serializers.EmailField()


# ---------------------------------------------------------------------------
# CareCircle agents (Settings CRUD over the internal Agent / CRM roster)
# ---------------------------------------------------------------------------
class PortalCrmAgentSerializer(serializers.ModelSerializer):
    """Full CRUD serializer for our internal CareCircle/CRM agents.

    Editable from Settings; the CallTools-synced identity fields are read-only
    so a manual edit never clobbers what the dialer sync owns.
    """

    group_label = serializers.CharField(source="get_group_display", read_only=True)

    class Meta:
        model = Agent
        fields = [
            "id",
            "name",
            "agent_code",
            "group",
            "group_label",
            "status",
            "cbo",
            "email",
            "first_name",
            "last_name",
            "title",
            "department",
            "is_manager",
            # Read-only CallTools identity / sync metadata.
            "username",
            "calltools_synced_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "group_label",
            "username",
            "calltools_synced_at",
            "created_at",
            "updated_at",
        ]

    def validate_agent_code(self, value):
        # Normalize empty string to NULL so the unique constraint treats
        # code-less agents as distinct (matches the model's nullable design).
        return (value or "").strip() or None


# ---------------------------------------------------------------------------
# Orders (purchase orders + delivery orders)
# ---------------------------------------------------------------------------
def _delivery_address_str(member):
    """Delivery address for a member = their active enrollment's address."""
    if member is None:
        return ""
    enr = active_enrollment(member)
    addr = enr.delivery_address if enr else None
    if not addr:
        return ""
    parts = [addr.street, addr.city, addr.state, addr.zip]
    line = ", ".join(p for p in [addr.street] if p)
    tail = " ".join(p for p in [addr.city, addr.state, addr.zip] if p)
    return ", ".join(p for p in [line, tail] if p) or ", ".join(p for p in parts if p)


class PortalDeliveryOrderSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    purchase_order_id = serializers.UUIDField(read_only=True)
    member_id = serializers.SerializerMethodField()
    member_name = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    kitchen_name = serializers.SerializerMethodField()
    menu_type_name = serializers.SerializerMethodField()
    custom_dietary_tags = serializers.SerializerMethodField()
    delivery_company_name = serializers.SerializerMethodField()
    delivery_address = serializers.SerializerMethodField()

    class Meta:
        model = DeliveryOrder
        fields = [
            "id", "purchase_order_id", "member_id", "member_name", "group_id",
            "status", "status_label", "expected_delivery_date", "delivered_at",
            "kitchen_name", "menu_type_name", "custom_dietary_tags",
            "delivery_company_name", "delivery_address", "proof_of_delivery",
        ]

    def get_member_id(self, obj):
        return str(obj.member_id) if obj.member_id else None

    def get_member_name(self, obj):
        return _full_name(obj.member) if obj.member else ""

    def get_group_id(self, obj):
        return str(obj.group_id) if obj.group_id else None

    def get_kitchen_name(self, obj):
        return obj.kitchen.name if obj.kitchen else ""

    def get_menu_type_name(self, obj):
        return obj.menu_type.name if obj.menu_type else ""

    def get_custom_dietary_tags(self, obj):
        return [t.name for t in obj.custom_dietary_tags.all()]

    def get_delivery_company_name(self, obj):
        return obj.delivery_company.name if obj.delivery_company else ""

    def get_delivery_address(self, obj):
        return _delivery_address_str(obj.member)


class PortalPurchaseOrderSerializer(serializers.ModelSerializer):
    """PO summary row for the global Orders list (+ counts)."""

    id = serializers.UUIDField(source="pk", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    kitchen_status_label = serializers.CharField(source="get_kitchen_status_display", read_only=True)
    delivery_status_label = serializers.CharField(source="get_delivery_status_display", read_only=True)
    kitchen_name = serializers.SerializerMethodField()
    delivery_company_name = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()

    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    split_from_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "po_number", "kind", "kind_label", "delivery_date", "po_date",
            "created_at", "sent_to_kitchen_at",
            "sent_to_delivery_at", "status", "status_label", "kitchen_status",
            "kitchen_status_label", "delivery_status", "delivery_status_label",
            "kitchen_name", "delivery_company_name", "split_from_id", "counts",
        ]

    def get_kitchen_name(self, obj):
        return obj.kitchen.name if obj.kitchen else ""

    def get_delivery_company_name(self, obj):
        return obj.delivery_company.name if obj.delivery_company else ""

    def get_counts(self, obj):
        orders = list(obj.delivery_orders.all())
        return {
            "total": len(orders),
            "delivered": sum(1 for o in orders if o.status == "delivered"),
            "failed": sum(1 for o in orders if o.status in ("failed", "returned")),
        }


class PortalMemberOrderSerializer(PortalPurchaseOrderSerializer):
    """PO with the current member's delivery orders embedded (member tab)."""

    delivery_orders = serializers.SerializerMethodField()

    class Meta(PortalPurchaseOrderSerializer.Meta):
        fields = PortalPurchaseOrderSerializer.Meta.fields + ["delivery_orders"]

    def get_delivery_orders(self, obj):
        member_id = self.context.get("member_id")
        orders = [o for o in obj.delivery_orders.all() if str(o.member_id) == str(member_id)]
        return PortalDeliveryOrderSerializer(orders, many=True, context=self.context).data


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------
class PortalHouseholdMemberSerializer(serializers.ModelSerializer):
    """A member row in the household tab, sourced from MemberDietaryProfile."""

    id = serializers.IntegerField(source="pk", read_only=True)
    client_id = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    mobile_number = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = MemberDietaryProfile
        fields = [
            "id", "client_id", "name", "mobile_number",
            "dietary_restrictions", "food_allergies", "other_dietary_restrictions",
            "meal_category", "menu_type", "general_verification_notes",
            "status", "status_label", "kitchen_meal_type", "kitchen_food_notes",
        ]

    def get_client_id(self, obj):
        return str(obj.client_id) if obj.client_id else None

    def get_name(self, obj):
        return obj.member_name or (_full_name(obj.client) if obj.client else "")

    def get_mobile_number(self, obj):
        # The member-app login number lives on the member's HouseholdMember row.
        membership = getattr(obj.client, "household_membership", None) if obj.client_id else None
        return (getattr(membership, "mobile_app_username", "") or "")


class PortalAddressEditSerializer(serializers.Serializer):
    street = serializers.CharField(allow_blank=True, required=False)
    unit = serializers.CharField(allow_blank=True, required=False, max_length=60)
    city = serializers.CharField(allow_blank=True, required=False)
    state = serializers.CharField(allow_blank=True, required=False, max_length=2)
    zip = serializers.CharField(allow_blank=True, required=False, max_length=10)
    notes = serializers.CharField(allow_blank=True, required=False)


class PortalMemberDietaryEditSerializer(serializers.Serializer):
    dietary_restrictions = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    food_allergies = serializers.ListField(child=serializers.CharField(), required=False)
    other_dietary_restrictions = serializers.CharField(allow_blank=True, required=False)
    meal_category = serializers.CharField(allow_blank=True, required=False)
    menu_type = serializers.CharField(allow_blank=True, required=False)
    general_verification_notes = serializers.CharField(allow_blank=True, required=False)
    # When true, return an out-of-orbit member to Active service. The view
    # re-runs the meal rule with the edited menu type/allergies and only
    # reactivates if the new combination can be fulfilled (it is not a model
    # field, so the view pops it before assigning the rest).
    reactivate = serializers.BooleanField(required=False, default=False)
    # When true, manually pull an active member Out of Orbit (agent override).
    # Excludes them from delivery schedules / Purchase Orders until reactivated.
    # Also a control flag, popped by the view before assigning model fields.
    deactivate = serializers.BooleanField(required=False, default=False)
    # When true, manually PAUSE an active member (agent override). Requires
    # ``pause_reason`` (stored as an agent note). Like Out of Orbit, paused
    # members are excluded from delivery schedules / Purchase Orders until
    # unpaused. Control flag, popped by the view before assigning model fields.
    pause = serializers.BooleanField(required=False, default=False)
    # When true, lift a member's manual pause. The view re-runs the meal rule so
    # the member returns to Active (or Out of Orbit if now unfulfillable).
    unpause = serializers.BooleanField(required=False, default=False)
    # Required free-text reason when pausing (also accepted on unpause). Stored
    # as an agent-authored note -- NOT a system note.
    pause_reason = serializers.CharField(allow_blank=True, required=False)

    def validate(self, attrs):
        if attrs.get("pause") and not (attrs.get("pause_reason") or "").strip():
            raise serializers.ValidationError(
                {"pause_reason": "A reason is required to pause this member."}
            )
        return attrs


# ---------------------------------------------------------------------------
# Settings: menu types, dietary tags, kitchens, delivery companies
# ---------------------------------------------------------------------------
def _mask_config(method, config):
    """Return a copy of an integration config with secrets masked for read."""
    out = dict(config or {})
    if out.get("apiKey"):
        out["apiKey"] = "********"
    return out


class PortalDietaryTagSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    type_label = serializers.CharField(source="get_type_display", read_only=True)
    usage_count = serializers.SerializerMethodField()

    class Meta:
        model = DietaryTag
        fields = ["id", "name", "type", "type_label", "usage_count"]

    def get_usage_count(self, obj):
        return obj.menu_type_tags.count()


class PortalMenuTypeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    tag_ids = serializers.SerializerMethodField()
    tags = PortalDietaryTagSerializer(many=True, read_only=True)

    class Meta:
        model = MenuType
        fields = ["id", "name", "is_active", "tag_ids", "tags"]

    def get_tag_ids(self, obj):
        return [str(t.pk) for t in obj.tags.all()]


class PortalCadenceSerializer(serializers.ModelSerializer):
    """Settings > Delivery Cadences: a configurable delivery cadence (label +
    the weekday codes it delivers on). ``code`` is a stable slug derived from
    the label on create when not supplied; it's read-only afterwards so the
    stored ProductType/schedule codes never drift."""

    id = serializers.UUIDField(source="pk", read_only=True)
    kitchen_count = serializers.SerializerMethodField()

    class Meta:
        model = Cadence
        fields = ["id", "code", "label", "weekdays", "is_active", "kitchen_count"]
        extra_kwargs = {"code": {"required": False}}

    def get_kitchen_count(self, obj):
        return obj.kitchens.count()

    def validate_weekdays(self, value):
        from ..models import CADENCE_WEEKDAY_CODES

        if not isinstance(value, list):
            raise serializers.ValidationError("weekdays must be a list of weekday codes.")
        bad = [w for w in value if w not in CADENCE_WEEKDAY_CODES]
        if bad:
            raise serializers.ValidationError(f"Invalid weekday code(s): {', '.join(bad)}.")
        # De-dupe while preserving canonical weekday order.
        return [w for w in CADENCE_WEEKDAY_CODES if w in value]

    def _slugify_code(self, label):
        from django.utils.text import slugify

        base = slugify(label).replace("-", "_")[:40] or "cadence"
        code, i = base, 2
        while Cadence.objects.filter(code=code).exists():
            code = f"{base}_{i}"[:40]
            i += 1
        return code

    def create(self, validated_data):
        if not validated_data.get("code"):
            validated_data["code"] = self._slugify_code(validated_data.get("label", ""))
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Code is immutable after creation (it keys stored schedules/products).
        validated_data.pop("code", None)
        return super().update(instance, validated_data)


class PortalKitchenIntegrationSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    config = serializers.SerializerMethodField()

    class Meta:
        model = KitchenIntegration
        fields = ["id", "method", "config"]

    def get_config(self, obj):
        return _mask_config(obj.method, obj.config)


class PortalKitchenMenuTypeSerializer(serializers.ModelSerializer):
    """Per-kitchen config for one menu type: price + allergies it can't manage."""

    menu_type_id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(source="menu_type.name", read_only=True)
    price = serializers.DecimalField(
        source="menu_type_price", max_digits=8, decimal_places=2, allow_null=True,
    )
    restriction_tag_ids = serializers.SerializerMethodField()

    class Meta:
        model = KitchenMenuType
        fields = ["menu_type_id", "name", "price", "restriction_tag_ids"]

    def get_restriction_tag_ids(self, obj):
        return [str(t.pk) for t in obj.restrictions.all()]


class PortalKitchenSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    menu_type_ids = serializers.SerializerMethodField()
    menu_types = PortalKitchenMenuTypeSerializer(
        source="kitchen_menu_types", many=True, read_only=True
    )
    supported_products = serializers.ListField(
        child=serializers.ChoiceField(choices=KitchenProductType.values),
        required=False,
    )
    product_options = serializers.SerializerMethodField()
    integrations = PortalKitchenIntegrationSerializer(many=True, read_only=True)
    cadence_ids = serializers.SerializerMethodField()
    cadences = PortalCadenceSerializer(many=True, read_only=True)

    class Meta:
        model = Kitchen
        fields = [
            "id", "name", "address", "phone", "email", "status", "status_label",
            "max_orders_per_day", "supported_products", "product_options",
            "menu_type_ids", "menu_types", "cadence_ids", "cadences",
            "integrations",
        ]

    def get_menu_type_ids(self, obj):
        return [str(m.pk) for m in obj.menu_types.all()]

    def get_product_options(self, obj):
        return [{"value": v, "label": l} for v, l in KitchenProductType.choices]

    def get_cadence_ids(self, obj):
        return [str(c.pk) for c in obj.cadences.all()]


class PortalDeliveryCompanyIntegrationSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    config = serializers.SerializerMethodField()

    class Meta:
        model = DeliveryCompanyIntegration
        fields = ["id", "method", "is_primary", "config"]

    def get_config(self, obj):
        return _mask_config(obj.method, obj.config)


class PortalDeliveryCompanySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    integrations = PortalDeliveryCompanyIntegrationSerializer(many=True, read_only=True)

    class Meta:
        model = DeliveryCompany
        fields = [
            "id", "name", "address", "phone", "email", "status", "status_label",
            "integrations",
        ]


# ---------------------------------------------------------------------------
# Verification wizard (create enrollment)
# ---------------------------------------------------------------------------
class VerificationMemberInputSerializer(serializers.Serializer):
    client_id = serializers.UUIDField(required=False, allow_null=True)
    member_name = serializers.CharField(required=False, allow_blank=True)
    # Mobile number the member will use to sign into the Benefully mobile app.
    # Wired to HouseholdMember.mobile_app_username when the member maps to a
    # household member (i.e. has a client_id).
    mobile_number = serializers.CharField(required=False, allow_blank=True)
    dietary_restrictions = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    food_allergies = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    other_dietary_restrictions = serializers.CharField(required=False, allow_blank=True)
    meal_category = serializers.CharField(required=False, allow_blank=True)
    menu_type = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class VerificationCreateSerializer(serializers.Serializer):
    program_name = serializers.CharField(allow_blank=True, required=False)
    members = VerificationMemberInputSerializer(many=True)
    # Delivery address
    street = serializers.CharField(allow_blank=True, required=False)
    apt = serializers.CharField(allow_blank=True, required=False)
    city = serializers.CharField(allow_blank=True, required=False)
    state = serializers.CharField(allow_blank=True, required=False, max_length=2)
    zip = serializers.CharField(allow_blank=True, required=False, max_length=10)
    address_notes = serializers.CharField(allow_blank=True, required=False)
    # Schedule
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)
    delivery_weekdays = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    # Validation
    is_family_verified = serializers.BooleanField(required=False, allow_null=True)
    medicaid_type_verified = serializers.BooleanField(required=False, allow_null=True)
    delivery_address_verified = serializers.BooleanField(required=False, allow_null=True)
    auth_status = serializers.ChoiceField(
        choices=["Draft", "Pending", "Accepted", "Denied"], default="Pending"
    )

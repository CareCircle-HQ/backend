"""Central client timeline (history) event emission.

``emit_timeline_event`` is the single low-level writer: it resolves the generic
entity link and writes the event once, keyed on ``dedupe_key``. Subsequent
saves / daily re-imports of the same entity are no-ops (the original event is
left untouched), so each domain occurrence is a single, stable timeline point.

The ``event_for_*`` builders translate each domain entity into the display
fields the manager timeline shows (title / subtitle / badge), then delegate to
``emit_timeline_event``. They are called from the capture points (extension bulk
endpoints + daily Unite Us pull). Each is a no-op when its precondition isn't
met (e.g. consent not accepted, no date available), so callers can fire them
unconditionally after an upsert.

NOTE: this is distinct from django-simple-history (api.history), which records
field-level audit diffs. The timeline is the curated, human-facing event stream.

Verification / authorization stage changes are emitted by
``event_for_verification``, called from ``api.services.lifecycle.advance_enrollment``
on every guarded stage transition.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from api.history import ChangeSource
from api.models import (
    CaseStatus,
    EnrollmentStage,
    RecordStatus,
    SocialCareCoverageStatus,
    TimelineBadgeTone,
    TimelineEvent,
    TimelineEventType,
)

logger = logging.getLogger(__name__)


def emit_timeline_event(
    *,
    client,
    event_type,
    occurred_at,
    title="",
    subtitle="",
    badge_text="",
    badge_tone=TimelineBadgeTone.NEUTRAL,
    source="",
    actor="",
    entity=None,
    enrollment=None,
    renewal_number=None,
    metadata=None,
    dedupe_key="",
):
    """Create a single timeline event the first time it's seen. Returns the
    event (new or pre-existing), or None when required data is missing
    (``client`` or ``occurred_at``).

    When ``dedupe_key`` is provided the write is **create-once**: the event is
    written the first time its source entity appears and is NOT re-stamped or
    updated on subsequent saves / daily re-imports. This keeps each domain
    occurrence (consent, screening, assessment, case, insurance, coverage) as a
    single, stable point on the timeline.
    """
    if client is None or occurred_at is None:
        return None

    content_type = None
    object_id = ""
    if entity is not None:
        content_type = ContentType.objects.get_for_model(entity.__class__)
        object_id = str(entity.pk)

    if renewal_number is None:
        renewal_number = enrollment.renewal_number if enrollment is not None else 1

    defaults = {
        "client": client,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "title": title or "",
        "subtitle": subtitle or "",
        "badge_text": badge_text or "",
        "badge_tone": badge_tone or TimelineBadgeTone.NEUTRAL,
        "source": source or "",
        "actor": actor or "",
        "content_type": content_type,
        "object_id": object_id,
        "enrollment": enrollment,
        "renewal_number": renewal_number,
        "metadata": metadata or {},
    }

    if dedupe_key:
        existing = TimelineEvent.objects.filter(dedupe_key=dedupe_key).first()
        if existing is not None:
            return existing  # create-once: leave the original event untouched
        return TimelineEvent.objects.create(dedupe_key=dedupe_key, **defaults)
    return TimelineEvent.objects.create(dedupe_key="", **defaults)


# ---------------------------------------------------------------------------
# Per-entity builders
# ---------------------------------------------------------------------------
def _client_full_name(client):
    return f"{client.first_name} {client.last_name}".strip()


def event_for_consent(client, *, source=ChangeSource.EXTENSION, actor=""):
    """Emit a 'Consent Granted' event once the client's consent is granted.

    The extension may signal consent via either the ``consent_accepted`` boolean
    or the ``consent_status`` string ('accepted') — treat either as granted
    (mirrors how ``services.lifecycle`` derives the consent stage).
    """
    if client is None:
        return None
    accepted = client.consent_accepted or (client.consent_status or "").lower() == "accepted"
    if not accepted:
        return None
    # One-time event: emit only the first time consent is granted. Once the
    # event exists, later profile saves must not re-emit / re-stamp it.
    if TimelineEvent.objects.filter(
        client=client, event_type=TimelineEventType.CONSENT_GRANTED
    ).exists():
        return None
    occurred = client.consented_at or client.created_at or timezone.now()
    signer = _client_full_name(client)
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.CONSENT_GRANTED,
        occurred_at=occurred,
        title="Consent Granted",
        subtitle=f"Consent signed by {signer}" if signer else "",
        source=source,
        actor=actor,
        entity=client,
        dedupe_key=f"consent_granted:{client.pk}",
    )


def event_for_screening(screening, *, source=ChangeSource.EXTENSION, actor=""):
    client = screening.client
    if client is None:
        return None
    occurred = screening.screen_created_at or screening.created_at
    needs = screening.identified_social_needs or []
    n = len(needs)
    status = (screening.screen_status or "").strip().lower()
    if n:
        badge_text = f"{n} unmet social need" + ("s" if n != 1 else "")
        tone = TimelineBadgeTone.WARNING
    else:
        badge_text = (screening.screen_status or "").replace("_", " ").title()
        if status.startswith("complete"):
            tone = TimelineBadgeTone.SUCCESS  # green: finished screening
        elif status in ("declined", "cancelled"):
            tone = TimelineBadgeTone.DANGER
        elif status == "expired":
            tone = TimelineBadgeTone.WARNING  # orange: needs re-screening
        else:
            tone = TimelineBadgeTone.NEUTRAL
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.SCREENING,
        occurred_at=occurred,
        title=screening.screen_type or "Screening",
        subtitle=screening.performing_organization_name or screening.provider_name or "",
        badge_text=badge_text,
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=screening,
        dedupe_key=f"screening:{screening.pk}",
    )


def event_for_assessment(assessment, *, source=ChangeSource.EXTENSION, actor=""):
    client = assessment.client
    if client is None:
        return None
    occurred = assessment.screen_created_at or assessment.created_at
    status = (assessment.eligible_status or "").strip()
    lower = status.lower()
    if status:
        if "ineligible" in lower or "not eligible" in lower:
            tone = TimelineBadgeTone.DANGER
        elif "eligible" in lower:
            tone = TimelineBadgeTone.SUCCESS
        else:
            tone = TimelineBadgeTone.INFO
    else:
        tone = TimelineBadgeTone.NEUTRAL
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.ASSESSMENT,
        occurred_at=occurred,
        title="Assessment",
        subtitle=assessment.performing_organization_name or assessment.provider_name or "",
        badge_text=status,
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=assessment,
        dedupe_key=f"assessment:{assessment.pk}",
    )


_CASE_TONE = {
    # Green: the case is actively open / being managed.
    CaseStatus.OPEN: TimelineBadgeTone.SUCCESS,
    CaseStatus.MANAGED: TimelineBadgeTone.SUCCESS,
    # Orange: needs attention or is no longer active.
    CaseStatus.PENDING_AUTHORIZATION: TimelineBadgeTone.WARNING,
    CaseStatus.CLOSED: TimelineBadgeTone.WARNING,
    # Red: cancelled.
    CaseStatus.CANCELLED: TimelineBadgeTone.DANGER,
}


def event_for_case(case, *, source=ChangeSource.EXTENSION, actor=""):
    client = case.client
    if client is None:
        return None
    occurred = (
        case.date_opened
        or case.case_processed_at
        or case.case_managed_at
        or case.updated_at
    )
    # Lead with the case classification (Internal Service / Navigation /
    # External Service / Eligibility) so multiple case events on one timeline are
    # distinguishable at a glance, followed by the provider.
    type_label = case.get_case_type_display()
    provider = case.provider_name or case.originating_provider_name or ""
    subtitle = " \u00b7 ".join(p for p in (type_label, provider) if p)
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.CASE_OPENED,
        occurred_at=occurred,
        title=case.program_name or case.service_type or "Case",
        subtitle=subtitle,
        badge_text=case.get_case_status_display(),
        badge_tone=_CASE_TONE.get(case.case_status, TimelineBadgeTone.NEUTRAL),
        source=source,
        actor=actor,
        entity=case,
        metadata={"case_type": case.case_type},
        dedupe_key=f"case_opened:{case.pk}",
    )


def event_for_insurance(insurance, *, source=ChangeSource.IMPORT, actor=""):
    client = insurance.client
    if client is None:
        return None
    occurred = insurance.enrolled_at or insurance.created_at
    status = insurance.status or insurance.record_status
    if status == RecordStatus.ACTIVE:
        tone = TimelineBadgeTone.SUCCESS  # green: active coverage
    elif status == RecordStatus.EXPIRED:
        tone = TimelineBadgeTone.WARNING  # orange: expired, needs renewal
    elif status == RecordStatus.INACTIVE:
        tone = TimelineBadgeTone.DANGER
    else:
        tone = TimelineBadgeTone.NEUTRAL
    member = insurance.external_member_id or insurance.insurance_id
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.INSURANCE,
        occurred_at=occurred,
        title=insurance.plan_name or insurance.get_plan_type_display(),
        subtitle=f"Member ID {member}" if member else "",
        badge_text=(insurance.get_status_display() if status else "")
        + (" \u00b7 Verified" if insurance.verified else ""),
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=insurance,
        metadata={"verified": insurance.verified},
        dedupe_key=f"insurance:{insurance.pk}",
    )


def event_for_social_care_coverage(coverage, *, source=ChangeSource.IMPORT, actor=""):
    client = coverage.client
    if client is None:
        return None
    occurred = coverage.enrolled_at or coverage.created_at
    if coverage.status == SocialCareCoverageStatus.ENROLLED:
        tone = TimelineBadgeTone.SUCCESS  # green: actively enrolled
    elif coverage.status == SocialCareCoverageStatus.EXPIRED:
        tone = TimelineBadgeTone.WARNING  # orange: expired, needs renewal
    else:
        tone = TimelineBadgeTone.NEUTRAL
    member = coverage.external_member_id
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.SOCIAL_CARE_COVERAGE,
        occurred_at=occurred,
        title=coverage.plan_name or "Social Care Coverage",
        subtitle=f"Member ID {member}" if member else "",
        badge_text=(coverage.get_status_display() if coverage.status else "")
        + (" \u00b7 Verified" if coverage.verified else ""),
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=coverage,
        metadata={"verified": coverage.verified},
        dedupe_key=f"social_care_coverage:{coverage.pk}",
    )


_VERIFICATION_STAGE_TONE = {
    EnrollmentStage.PENDING_VALIDATION: TimelineBadgeTone.NEUTRAL,
    EnrollmentStage.VALIDATED: TimelineBadgeTone.INFO,
    EnrollmentStage.PENDING_VERIFICATION: TimelineBadgeTone.NEUTRAL,
    EnrollmentStage.VERIFIED: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.WAITING_AUTHORIZATION: TimelineBadgeTone.WARNING,
    EnrollmentStage.AUTHORIZED: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.DENIED: TimelineBadgeTone.DANGER,
    EnrollmentStage.SERVICE_ACTIVE: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.SERVICE_COMPLETE: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.ON_HOLD: TimelineBadgeTone.WARNING,
    EnrollmentStage.CLOSED: TimelineBadgeTone.NEUTRAL,
    EnrollmentStage.CANCELLED: TimelineBadgeTone.DANGER,
}


# Each enrollment stage maps to its own granular (timeline event type, title).
# One distinct TimelineEventType per stage so the History tab can filter and
# read each transition precisely instead of collapsing them into generic
# "Verification" / "Service" rows.
_STAGE_TIMELINE = {
    EnrollmentStage.PENDING_VALIDATION: (TimelineEventType.PENDING_VALIDATION, "Pending Validation"),
    EnrollmentStage.VALIDATED: (TimelineEventType.VALIDATED, "Validated"),
    EnrollmentStage.PENDING_VERIFICATION: (TimelineEventType.VERIFICATION_REQUESTED, "Verification Requested"),
    EnrollmentStage.VERIFIED: (TimelineEventType.VERIFICATION_COMPLETED, "Verification Completed"),
    EnrollmentStage.WAITING_AUTHORIZATION: (TimelineEventType.WAITING_AUTHORIZATION, "Waiting Authorization"),
    EnrollmentStage.AUTHORIZED: (TimelineEventType.AUTHORIZED, "Authorized"),
    EnrollmentStage.DENIED: (TimelineEventType.DENIED, "Denied"),
    EnrollmentStage.KITCHEN_ASSIGNMENT: (TimelineEventType.KITCHEN_ASSIGNED, "Kitchen Assigned"),
    EnrollmentStage.SERVICE_ACTIVE: (TimelineEventType.SERVICE_ACTIVATED, "Service Activated"),
    EnrollmentStage.SERVICE_COMPLETE: (TimelineEventType.SERVICE_COMPLETED, "Service Completed"),
    EnrollmentStage.ON_HOLD: (TimelineEventType.SERVICE_ON_HOLD, "Service On Hold"),
    EnrollmentStage.CLOSED: (TimelineEventType.SERVICE_CLOSED, "Service Closed"),
    EnrollmentStage.CANCELLED: (TimelineEventType.SERVICE_CANCELLED, "Service Cancelled"),
}


def stage_timeline_fields(stage, *, from_stage=None):
    """(event_type, title) for an enrollment stage. ``from_stage`` lets a
    transition read more naturally (e.g. resuming from hold = 'Service Resumed',
    which is its own granular event type). Returns None when the stage is
    unknown."""
    try:
        stage = EnrollmentStage(stage)
    except ValueError:
        return None
    if stage == EnrollmentStage.SERVICE_ACTIVE and from_stage == EnrollmentStage.ON_HOLD:
        return TimelineEventType.SERVICE_RESUMED, "Service Resumed"
    return _STAGE_TIMELINE.get(stage, (TimelineEventType.VERIFICATION, stage.label))


def event_for_verification(enrollment, *, stage_event=None, source=ChangeSource.SYSTEM, actor=""):
    """Emit a timeline event for an enrollment stage change.

    Called from :func:`api.services.lifecycle.advance_enrollment` after a
    transition. When ``stage_event`` is supplied the write is keyed on that
    StageEvent (one timeline row per transition); otherwise it logs unconditionally.
    The event type + title reflect the specific stage (Verification vs Service),
    so hold/resume and other service changes read as their own events.
    """
    client = enrollment.client
    if client is None:
        return None
    occurred = enrollment.stage_at or timezone.now()
    try:
        label = EnrollmentStage(enrollment.stage).label
    except ValueError:
        label = (enrollment.stage or "").replace("_", " ").title()
    from_stage = stage_event.from_stage if stage_event is not None else None
    fields = stage_timeline_fields(enrollment.stage, from_stage=from_stage)
    event_type, title = fields if fields else (TimelineEventType.VERIFICATION, label or "Verification")
    tone = _VERIFICATION_STAGE_TONE.get(enrollment.stage, TimelineBadgeTone.NEUTRAL)
    dedupe = f"verification_stage:{stage_event.pk}" if stage_event is not None else ""
    return emit_timeline_event(
        client=client,
        event_type=event_type,
        occurred_at=occurred,
        title=title,
        subtitle=enrollment.program_name or "",
        badge_text=label,
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        dedupe_key=dedupe,
    )


_TICKET_SEVERITY_TONE = {
    "high": TimelineBadgeTone.DANGER,
    "medium": TimelineBadgeTone.WARNING,
    "low": TimelineBadgeTone.INFO,
}


def event_for_ticket_created(ticket, *, source=ChangeSource.CRM, actor=""):
    """Emit a 'New Ticket Created' event the first time a ticket is opened for a
    client. No-op for client-less tickets (e.g. member-not-found). De-duped on
    the ticket pk so re-saves don't duplicate the row."""
    client = ticket.client
    if client is None:
        return None
    occurred = ticket.created_at or timezone.now()
    type_label = ticket.type.label if ticket.type_id else "Ticket"
    severity = (ticket.severity or "").lower()
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.TICKET_CREATED,
        occurred_at=occurred,
        title="New Ticket Created",
        subtitle=ticket.reason or type_label,
        badge_text=ticket.get_severity_display() if ticket.severity else "",
        badge_tone=_TICKET_SEVERITY_TONE.get(severity, TimelineBadgeTone.NEUTRAL),
        source=source,
        actor=actor,
        entity=ticket,
        metadata={
            "ticket_type": type_label,
            "severity": severity,
            "ticket_source": ticket.source or "",
        },
        dedupe_key=f"ticket_created:{ticket.pk}",
    )


def _format_address(address):
    region = " ".join(p for p in (address.state, address.zip) if p)
    unit = getattr(address, "unit", "")
    return ", ".join(
        p for p in (address.street, unit, address.city, region) if p
    )


def event_for_delivery_address_change(
    client, address, *, previous="", enrollment=None,
    source=ChangeSource.CRM, actor="",
):
    """Emit a 'Delivery Address Changed' event. Not de-duped (each change is its
    own timeline point); ``previous`` is the pre-edit address string."""
    if client is None or address is None:
        return None
    new_addr = _format_address(address)
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.DELIVERY_ADDRESS_CHANGED,
        occurred_at=timezone.now(),
        title="Delivery Address Changed",
        subtitle=new_addr,
        source=source,
        actor=actor,
        entity=address,
        enrollment=enrollment,
        metadata={"previous": previous, "new": new_addr},
    )


def event_for_out_of_orbit(
    profile, *, enrollment=None, reason="", source=ChangeSource.SYSTEM, actor="",
):
    """Emit a 'Household set as Out of Orbit' event for the member whose dietary
    data (menu type + allergies) can't be safely fulfilled. Logged on the
    member's own client; de-duped per enrollment so re-running the kitchen
    assignment doesn't duplicate the row."""
    client = getattr(profile, "client", None)
    if client is None:
        return None
    enrollment = enrollment or getattr(profile, "enrollment", None)
    dedupe = f"out_of_orbit:{enrollment.pk}:{profile.pk}" if enrollment is not None else ""
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.OUT_OF_ORBIT,
        occurred_at=timezone.now(),
        title="Household set as Out of Orbit",
        subtitle=profile.member_name or "",
        badge_text="Out of Orbit",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"reason": reason, "menu_type": profile.menu_type or ""},
        dedupe_key=dedupe,
    )


def event_for_member_reactivated(
    profile, *, enrollment=None, source=ChangeSource.SYSTEM, actor="",
):
    """Emit a 'Member reactivated' event when an out-of-orbit member is returned
    to Active service (e.g. an agent picked a fulfillable menu type). Logged on
    the member's own client. Not de-duped, so each deactivate/reactivate cycle
    is recorded."""
    client = getattr(profile, "client", None)
    if client is None:
        return None
    enrollment = enrollment or getattr(profile, "enrollment", None)
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_REACTIVATED,
        occurred_at=timezone.now(),
        title="Member reactivated",
        subtitle=profile.member_name or "",
        badge_text="Active",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"menu_type": profile.menu_type or ""},
    )

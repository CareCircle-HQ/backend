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
    if n:
        badge_text = f"{n} unmet social need" + ("s" if n != 1 else "")
        tone = TimelineBadgeTone.WARNING
    else:
        badge_text = (screening.screen_status or "").replace("_", " ").title()
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
    CaseStatus.OPEN: TimelineBadgeTone.SUCCESS,
    CaseStatus.PENDING_AUTHORIZATION: TimelineBadgeTone.WARNING,
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
        tone = TimelineBadgeTone.SUCCESS
    elif status in (RecordStatus.EXPIRED, RecordStatus.INACTIVE):
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
        tone = TimelineBadgeTone.SUCCESS
    elif coverage.status == SocialCareCoverageStatus.EXPIRED:
        tone = TimelineBadgeTone.DANGER
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


def event_for_verification(enrollment, *, stage_event=None, source=ChangeSource.SYSTEM, actor=""):
    """Emit a timeline event for an enrollment verification stage change.

    Called from :func:`api.services.lifecycle.advance_enrollment` after a
    transition. When ``stage_event`` is supplied the write is keyed on that
    StageEvent (one timeline row per transition); otherwise it logs unconditionally.
    """
    client = enrollment.client
    if client is None:
        return None
    occurred = enrollment.stage_at or timezone.now()
    try:
        label = EnrollmentStage(enrollment.stage).label
    except ValueError:
        label = (enrollment.stage or "").replace("_", " ").title()
    tone = _VERIFICATION_STAGE_TONE.get(enrollment.stage, TimelineBadgeTone.NEUTRAL)
    dedupe = f"verification_stage:{stage_event.pk}" if stage_event is not None else ""
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.VERIFICATION,
        occurred_at=occurred,
        title=enrollment.program_name or "Verification",
        subtitle=f"Stage changed to {label}",
        badge_text=label,
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        dedupe_key=dedupe,
    )

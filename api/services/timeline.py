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

import hashlib
import logging

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from api.history import ChangeSource
from api.models import (
    CaseStatus,
    CaseType,
    EnrollmentStage,
    RecordStatus,
    SocialCareCoverageStatus,
    TimelineBadgeTone,
    TimelineEvent,
    TimelineEventType,
)

logger = logging.getLogger(__name__)

# The ``TimelineEvent.dedupe_key`` column is ``varchar(128)``. Some keys are
# built from several UUIDs (e.g. the governing-case-changed key concatenates the
# client id + previous + new case ids -> 133 chars), which would overflow the
# column and raise ``DataError`` on INSERT -- aborting whatever reconcile emitted
# the event. Clamp any over-long key to a stable, collision-resistant form so the
# create-once dedupe still holds and no caller can ever be broken by a long key.
_DEDUPE_KEY_MAX = 128


def _clamp_dedupe_key(dedupe_key):
    """Return ``dedupe_key`` unchanged when it fits the column, else a
    deterministic <=128-char form: a readable prefix + a SHA-1 of the full key.

    Deterministic (same input -> same output) so the unique/create-once semantics
    are preserved for a given logical key."""
    if len(dedupe_key) <= _DEDUPE_KEY_MAX:
        return dedupe_key
    digest = hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()  # 40 chars
    # prefix + ":" + digest == _DEDUPE_KEY_MAX exactly.
    prefix = dedupe_key[: _DEDUPE_KEY_MAX - len(digest) - 1]
    return f"{prefix}:{digest}"


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
    case=None,
    renewal_number=None,
    metadata=None,
    dedupe_key="",
    update_metadata=False,
):
    """Create a single timeline event the first time it's seen. Returns the
    event (new or pre-existing), or None when required data is missing
    (``client`` or ``occurred_at``).

    When ``dedupe_key`` is provided the write is **create-once**: the event is
    written the first time its source entity appears and is NOT re-stamped or
    updated on subsequent saves / daily re-imports. This keeps each domain
    occurrence (consent, screening, assessment, case, insurance, coverage) as a
    single, stable point on the timeline.

    ``update_metadata`` is a narrow, opt-in exception to create-once: when True
    and the event already exists, the supplied ``metadata`` keys are merged into
    the existing row (title / date / dedupe_key stay stable). This lets a later
    pass back-fill data that wasn't available when the row was first written --
    e.g. an assessment's ``eligible_services`` arriving from the enrichment pull
    after the CSV import created a results-less row. Only the passed keys are
    touched; other metadata is preserved.
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
        "case": case,
        "renewal_number": renewal_number,
        "metadata": metadata or {},
    }

    if dedupe_key:
        dedupe_key = _clamp_dedupe_key(dedupe_key)
        existing = TimelineEvent.objects.filter(dedupe_key=dedupe_key).first()
        if existing is not None:
            if update_metadata and metadata:
                merged = {**(existing.metadata or {}), **metadata}
                if merged != existing.metadata:
                    existing.metadata = merged
                    existing.save(update_fields=["metadata"])
            return existing  # create-once: row identity is left untouched
        return TimelineEvent.objects.create(dedupe_key=dedupe_key, **defaults)
    return TimelineEvent.objects.create(dedupe_key="", **defaults)


# ---------------------------------------------------------------------------
# Per-entity builders
# ---------------------------------------------------------------------------
def _client_full_name(client):
    return f"{client.first_name} {client.last_name}".strip()


def _norm_value(value):
    """Normalize a value for change-detection. Lists/tuples/sets compare
    order-insensitively; scalars compare as trimmed strings."""
    if isinstance(value, (list, tuple, set)):
        return sorted(str(v) for v in value)
    return "" if value is None else str(value).strip()


def _display_value(value):
    """Human-readable rendering of a value for a change row. Lists join with
    ', '; empty values render as an em dash."""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value) or "\u2014"
    text = "" if value is None else str(value).strip()
    return text or "\u2014"


def build_change_list(pairs):
    """Build the standardized ``changes`` metadata list from ``pairs``.

    ``pairs`` is an iterable of ``(label, before, after)``. Only genuinely
    changed entries are kept (order-insensitive for lists), each rendered as
    ``{"field": label, "from": <display>, "to": <display>}`` so the History tab
    can show a clean ``from -> to`` diff. Returns ``[]`` when nothing changed.
    """
    changes = []
    for label, before, after in pairs:
        if _norm_value(before) != _norm_value(after):
            changes.append({
                "field": label,
                "from": _display_value(before),
                "to": _display_value(after),
            })
    return changes


def _service_names(services):
    """Normalize an ``eligible_services`` JSON blob to a flat list of service-name
    strings. Entries may be plain strings (``["Medicaid", "SNAP"]``) or dicts
    (``{"name"|"service"|"label"|"service_type": ...}``)."""
    out = []
    for s in services or []:
        if isinstance(s, dict):
            name = (
                s.get("name") or s.get("service") or s.get("label")
                or s.get("service_type") or ""
            )
            if name:
                out.append(str(name))
        elif s:
            out.append(str(s))
    return out


def _social_need_names(needs):
    """Normalize ``identified_social_needs`` to a flat list of names. Entries may
    be plain strings or dicts (``{"name"|"identified_social_need_name"|"code"}``)."""
    out = []
    for n in needs or []:
        if isinstance(n, dict):
            name = (
                n.get("name") or n.get("identified_social_need_name")
                or n.get("code") or ""
            )
            if name:
                out.append(str(name))
        elif n:
            out.append(str(n))
    return out


def _case_product_kind(case):
    """'meals' / 'boxes' / '' for an internal-service case, by mapping its
    program/service name through the catalog keyword rules."""
    if case.case_type != CaseType.INTERNAL_SERVICE:
        return ""
    from api.services.catalog import product_type_kind_for_name

    kind = (
        product_type_kind_for_name(case.program_name or "")
        or product_type_kind_for_name(case.service_type or "")
    )
    return kind or ""


def _is_governing_case(case):
    """True when ``case`` is the client's current GOVERNING internal-service case
    (an approved authorization outranks a denied/pending one). Snapshotted onto
    the case timeline event so the history shows which case was driving service
    at the time. Lazy import avoids a lifecycle<->timeline import cycle."""
    if case.case_type != CaseType.INTERNAL_SERVICE or case.client_id is None:
        return False
    from api.services.lifecycle import (
        governing_case_key,
        open_internal_service_cases,
    )

    cases = open_internal_service_cases(case.client)
    if not cases:
        return False
    governing = max(cases, key=governing_case_key)
    return str(governing.case_id) == str(case.case_id)


def _auth_window(case):
    """``(start_iso, end_iso)`` for the case's effective authorization window
    (approval window, falling back to the request window on an approved case).
    Empty strings when a bound isn't set."""
    start, end = case.effective_authorization_window()
    return (
        start.isoformat() if start else "",
        end.isoformat() if end else "",
    )


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


def event_for_consent_withdrawn(client, *, reason="", occurred_at=None,
                                source=ChangeSource.EXTENSION, actor=""):
    """Emit a 'Consent Withdrawn' event when a previously-consented client's
    consent is revoked. Unlike the grant (create-once), a withdrawal is a real,
    dated occurrence -- keyed by its timestamp so a later re-consent/re-withdraw
    each records distinctly."""
    if client is None:
        return None
    occurred = occurred_at or timezone.now()
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.CONSENT_WITHDRAWN,
        occurred_at=occurred,
        title="Consent Withdrawn",
        subtitle=reason or "Consent revoked in Unite Us.",
        source=source,
        actor=actor,
        entity=client,
        dedupe_key=f"consent_withdrawn:{client.pk}:{occurred.isoformat()}",
    )


def event_for_screening(screening, *, source=ChangeSource.EXTENSION, actor="", resync=False):
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
        metadata={
            # What the member was found eligible for + the screening results, so
            # the History tab shows outcomes without opening the Screenings tab.
            # Full Q&A stays on the Screening entity (linked via entity_id).
            "eligible_status": screening.eligible_status or "",
            "eligible_services": _service_names(screening.eligible_services),
            "identified_social_needs": _social_need_names(needs),
            "results_count": len(screening.questions_answers or []),
        },
        dedupe_key=f"screening:{screening.pk}",
        update_metadata=resync,
    )


def event_for_assessment(assessment, *, source=ChangeSource.EXTENSION, actor="", resync=False):
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
        metadata={
            # What the member was found eligible for + the assessment results.
            # ``eligible_services`` often arrives AFTER the CSV import creates the
            # row (via the screenings-ingestion enrichment pull), so this builder
            # is re-invoked with resync=True there to back-fill it. Full Q&A stays
            # on the Assessment entity (linked via entity_id).
            "eligible_status": assessment.eligible_status or "",
            "eligible_services": _service_names(assessment.eligible_services),
            "form_name": assessment.form_name or "",
            "results_count": len(assessment.questions_answers or []),
        },
        dedupe_key=f"assessment:{assessment.pk}",
        update_metadata=resync,
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
    auth_start, auth_end = _auth_window(case)
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
        case=case,
        metadata={
            "case_type": case.case_type,
            # Meals vs boxes (internal-service cases), whether this case is the
            # one currently GOVERNING service, and the authorization status +
            # window. Auth state changes over time, so this row's metadata is
            # re-synced on each re-import (update_metadata=True) to stay current
            # while keeping a single stable "Case" point on the timeline.
            "product_kind": _case_product_kind(case),
            "is_governing": _is_governing_case(case),
            "auth_status": case.service_authorization_status or "",
            "auth_status_label": (
                case.service_authorization_status_label
                or (case.service_authorization_status or "").replace("_", " ").title()
            ),
            "auth_window_start": auth_start,
            "auth_window_end": auth_end,
        },
        dedupe_key=f"case_opened:{case.pk}",
        update_metadata=True,
    )


def event_for_case_status_change(
    case, *, previous_status="", source=ChangeSource.EXTENSION, actor="",
    import_run=None,
):
    """Emit a 'Case Status Changed' event when a case's status transitions
    (e.g. Open -> Closed / Cancelled / Managed). One row per transition, keyed
    on (case, new status, day) so a re-import of an unchanged file is a no-op."""
    client = case.client
    if client is None:
        return None
    occurred = case.case_closed_at or case.updated_at or timezone.now()
    new_label = case.get_case_status_display()
    prev_label = (previous_status or "").replace("_", " ").title()
    subtitle = f"{prev_label} \u2192 {new_label}" if prev_label else new_label
    # Explain WHY the case was closed/cancelled on the timeline, mirroring the
    # reason recorded in the client note. The closure reason lives on the case's
    # ``closed_note`` (populated by the CSV/API import + the extension); fall back
    # to the case description. Only appended for a terminal transition so open
    # cases stay a clean "Prev -> New".
    reason = (case.closed_note or "").strip() or (case.case_description or "").strip()
    if reason and case.case_status in (CaseStatus.CLOSED, CaseStatus.CANCELLED):
        subtitle = f"{subtitle} \u00b7 {reason}" if subtitle else reason
    day = occurred.date().isoformat() if occurred else ""
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.CASE_STATUS_CHANGED,
        occurred_at=occurred,
        title="Case Status Changed",
        subtitle=subtitle,
        badge_text=new_label,
        badge_tone=_CASE_TONE.get(case.case_status, TimelineBadgeTone.NEUTRAL),
        source=source,
        actor=actor,
        entity=case,
        case=case,
        metadata={
            "previous_status": previous_status or "",
            "new_status": case.case_status,
            "closed_reason": (case.closed_note or "").strip(),
            "product_kind": _case_product_kind(case),
            "import_run": import_run.pk if import_run is not None else None,
        },
        dedupe_key=f"case_status:{case.pk}:{case.case_status}:{day}",
    )


_AUTH_TONE = {
    "approved": TimelineBadgeTone.SUCCESS,
    "not_required": TimelineBadgeTone.SUCCESS,
    "pending": TimelineBadgeTone.WARNING,
    "expired": TimelineBadgeTone.WARNING,
    "denied": TimelineBadgeTone.DANGER,
}


def event_for_case_authorization_change(
    case, *, previous_auth="", source=ChangeSource.EXTENSION, actor="",
    import_run=None,
):
    """Emit a 'Case Authorization Changed' event when a case's service
    authorization status transitions (approved / denied / pending / expired).
    One row per transition, keyed on (case, new auth, day)."""
    client = case.client
    if client is None:
        return None
    new_auth = case.service_authorization_status or ""
    occurred = (
        case.service_authorization_approval_starts_at
        or case.updated_at
        or timezone.now()
    )
    new_label = (case.service_authorization_status_label
                 or new_auth.replace("_", " ").title())
    prev_label = (previous_auth or "").replace("_", " ").title()
    subtitle = f"{prev_label} \u2192 {new_label}" if prev_label else new_label
    day = occurred.date().isoformat() if occurred else ""
    auth_start, auth_end = _auth_window(case)
    # Surface the authorization window on the transition subtitle so the history
    # reads "Pending -> Approved · 2026-02-01 -> 2027-01-31" at a glance.
    if auth_start or auth_end:
        window = f"{auth_start or '?'} \u2192 {auth_end or '?'}"
        subtitle = f"{subtitle} \u00b7 {window}"
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.CASE_AUTH_CHANGED,
        occurred_at=occurred,
        title="Authorization Changed",
        subtitle=subtitle,
        badge_text=new_label,
        badge_tone=_AUTH_TONE.get(new_auth, TimelineBadgeTone.NEUTRAL),
        source=source,
        actor=actor,
        entity=case,
        case=case,
        metadata={
            "previous_auth": previous_auth or "",
            "new_auth": new_auth,
            # The authorization WINDOW that this decision established, plus the
            # meals/boxes product it authorizes -- the core "what/when" of an auth.
            "auth_window_start": auth_start,
            "auth_window_end": auth_end,
            "product_kind": _case_product_kind(case),
            "authorized_amount": case.authorized_amount or "",
            "import_run": import_run.pk if import_run is not None else None,
        },
        dedupe_key=f"case_auth:{case.pk}:{new_auth}:{day}",
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
    # Medicaid-type rule is client-level (some Medicaid subtypes -- MLTC/MAP/FFS
    # -- aren't served); surface whether the member meets it alongside the plan.
    from api.services import eligibility

    medicaid_note = eligibility.medicaid_type_reason(client)
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
        metadata={
            "verified": insurance.verified,
            "plan_type": insurance.plan_type or "",
            "plan_type_label": (
                insurance.get_plan_type_display() if insurance.plan_type else ""
            ),
            "status": insurance.status or insurance.record_status or "",
            "is_primary": insurance.is_primary,
            "enrolled_at": insurance.enrolled_at.isoformat() if insurance.enrolled_at else "",
            "expired_at": insurance.expired_at.isoformat() if insurance.expired_at else "",
            "expired": eligibility.coverage_expired(insurance.expired_at),
            # True when the member's Medicaid type is one we serve.
            "meets_medicaid_rule": not medicaid_note,
            "medicaid_rule_note": medicaid_note,  # "" when the rule is met
        },
        dedupe_key=f"insurance:{insurance.pk}",
        update_metadata=True,
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
    from api.services import eligibility

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
        metadata={
            "verified": coverage.verified,
            "plan_type": coverage.plan_type or "",
            "status": coverage.status or "",
            "enrolled_at": coverage.enrolled_at.isoformat() if coverage.enrolled_at else "",
            "expired_at": coverage.expired_at.isoformat() if coverage.expired_at else "",
            "expired": eligibility.coverage_expired(coverage.expired_at),
        },
        dedupe_key=f"social_care_coverage:{coverage.pk}",
        update_metadata=True,
    )


_VERIFICATION_STAGE_TONE = {
    EnrollmentStage.PENDING_VALIDATION: TimelineBadgeTone.NEUTRAL,
    EnrollmentStage.VALIDATED: TimelineBadgeTone.INFO,
    EnrollmentStage.PENDING_VERIFICATION: TimelineBadgeTone.NEUTRAL,
    EnrollmentStage.VERIFIED: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.KITCHEN_ASSIGNMENT: TimelineBadgeTone.INFO,
    EnrollmentStage.SERVICE_ACTIVE: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.SERVICE_COMPLETE: TimelineBadgeTone.SUCCESS,
    EnrollmentStage.ON_HOLD: TimelineBadgeTone.WARNING,
    EnrollmentStage.CLOSED: TimelineBadgeTone.NEUTRAL,
    EnrollmentStage.CANCELLED: TimelineBadgeTone.DANGER,
    EnrollmentStage.DISREGARDED: TimelineBadgeTone.WARNING,
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
    EnrollmentStage.KITCHEN_ASSIGNMENT: (TimelineEventType.AWAITING_KITCHEN, "Awaiting Kitchen Assignment"),
    EnrollmentStage.SERVICE_ACTIVE: (TimelineEventType.SERVICE_ACTIVATED, "Service Activated"),
    EnrollmentStage.SERVICE_COMPLETE: (TimelineEventType.SERVICE_COMPLETED, "Service Completed"),
    EnrollmentStage.ON_HOLD: (TimelineEventType.SERVICE_ON_HOLD, "Service On Hold"),
    EnrollmentStage.CLOSED: (TimelineEventType.SERVICE_CLOSED, "Service Closed"),
    EnrollmentStage.CANCELLED: (TimelineEventType.SERVICE_CANCELLED, "Service Cancelled"),
    EnrollmentStage.DISREGARDED: (
        TimelineEventType.VERIFICATION_DISREGARDED, "Verification Request Disregarded",
    ),
}


def stage_timeline_fields(stage, *, from_stage=None, trigger=""):
    """(event_type, title) for an enrollment stage. ``from_stage`` lets a
    transition read more naturally (e.g. resuming from hold = 'Service Resumed',
    which is its own granular event type). ``trigger`` distinguishes a carried
    transition from a genuine one. Returns None when the stage is unknown."""
    try:
        stage = EnrollmentStage(stage)
    except ValueError:
        return None
    if stage == EnrollmentStage.SERVICE_ACTIVE and from_stage == EnrollmentStage.ON_HOLD:
        return TimelineEventType.SERVICE_RESUMED, "Service Resumed"
    # A governing-case replacement CARRIES the household's existing verification
    # onto the new/reused enrollment -- it is NOT a fresh verification, so label
    # it as a carry-over (keeps the VERIFICATION_COMPLETED type for filtering).
    if stage == EnrollmentStage.VERIFIED and trigger == "case_replaced":
        return (
            TimelineEventType.VERIFICATION_COMPLETED,
            "Verification Data Carried over new enrollment",
        )
    return _STAGE_TIMELINE.get(stage, (TimelineEventType.VERIFICATION, stage.label))


def _enrollment_member_names(enrollment):
    """Roster of member names captured on an enrollment's household (the members
    on the verification), for the verification timeline events."""
    if enrollment is None:
        return []
    try:
        return [
            (mp.member_name or "").strip()
            for mp in enrollment.member_profiles.all()
            if (mp.member_name or "").strip()
        ]
    except Exception:  # noqa: BLE001 - never let history-logging break a save
        return []


def _governing_case_id_for(enrollment):
    """The id (str) of the internal-service case that GOVERNS this enrollment --
    the same case the rest of the app treats as authoritative -- falling back to
    the enrollment's tied case. Lazy import avoids a lifecycle<->timeline cycle."""
    if enrollment is None:
        return ""
    try:
        from api.services.lifecycle import governing_internal_case

        case = governing_internal_case(enrollment) or enrollment.case
    except Exception:  # noqa: BLE001
        case = getattr(enrollment, "case", None)
    return str(case.case_id) if case is not None else ""


def event_for_verification(enrollment, *, stage_event=None, source=ChangeSource.SYSTEM,
                           actor="", trigger=""):
    """Emit a timeline event for an enrollment stage change.

    Called from :func:`api.services.lifecycle.advance_enrollment` after a
    transition. When ``stage_event`` is supplied the write is keyed on that
    StageEvent (one timeline row per transition); otherwise it logs unconditionally.
    The event type + title reflect the specific stage (Verification vs Service),
    so hold/resume and other service changes read as their own events.

    The event ``metadata`` records the FULL context of the change -- previous +
    new stage (value + label), the ``trigger`` (what caused it), the reason note
    and the acting label -- so the history can be traced to its cause (e.g. why
    the system placed a member On Hold).
    """
    client = enrollment.client
    if client is None:
        return None
    # 'Verification Disregarded' is intentionally NOT surfaced on the timeline:
    # the disregard action still transitions the enrollment + writes a Note, but
    # it no longer produces a history event.
    if enrollment.stage == EnrollmentStage.DISREGARDED:
        return None
    occurred = enrollment.stage_at or timezone.now()
    try:
        label = EnrollmentStage(enrollment.stage).label
    except ValueError:
        label = (enrollment.stage or "").replace("_", " ").title()
    from_stage = stage_event.from_stage if stage_event is not None else None
    eff_trigger = trigger or ((stage_event.metadata or {}).get("trigger", "") if stage_event is not None else "")
    fields = stage_timeline_fields(enrollment.stage, from_stage=from_stage, trigger=eff_trigger)
    event_type, title = fields if fields else (TimelineEventType.VERIFICATION, label or "Verification")
    carried_verification = (
        enrollment.stage == EnrollmentStage.VERIFIED and eff_trigger == "case_replaced"
    )
    tone = _VERIFICATION_STAGE_TONE.get(enrollment.stage, TimelineBadgeTone.NEUTRAL)
    dedupe = f"verification_stage:{stage_event.pk}" if stage_event is not None else ""
    # For an off-ramp (Disregarded / Cancelled / Closed), surface the agent's
    # reason (recorded on the StageEvent note) directly on the timeline row
    # instead of the program name, so the history explains WHY -- mirroring the
    # reason captured in the client note.
    subtitle = enrollment.program_name or ""
    _REASON_STAGES = (
        EnrollmentStage.DISREGARDED,
        EnrollmentStage.CANCELLED,
        EnrollmentStage.CLOSED,
        # On Hold carries the reason (e.g. a Nutritionist hold) so the timeline
        # explains WHY the household was held, mirroring the client note.
        EnrollmentStage.ON_HOLD,
    )
    note = stage_event.note if stage_event is not None else ""
    if enrollment.stage in _REASON_STAGES and note:
        subtitle = note
    # Carried verification: spell out WHY the household reads verified on this new
    # enrollment (the capture was carried from the superseded one, not re-done),
    # naming BOTH governing cases involved in the switch.
    carried_from_case = ""
    carried_to_case = str(enrollment.case_id) if enrollment.case_id else ""
    if carried_verification:
        old = getattr(enrollment, "supersedes", None)
        ctx = (getattr(old, "close_context", None) or {}) if old is not None else {}
        carried_from_case = str(
            ctx.get("previous_case_id") or (getattr(old, "case_id", "") or "") or ""
        )
        carried_to_case = carried_to_case or str(ctx.get("new_case_id") or "")
        subtitle = (
            "Verification data carried from the previous enrollment during a "
            f"governing-case change (case {carried_from_case or 'n/a'} \u2192 "
            f"{carried_to_case or 'n/a'}); not a new verification."
        )

    def _stage_label(value):
        if not value:
            return ""
        try:
            return EnrollmentStage(value).label
        except ValueError:
            return str(value).replace("_", " ").title()

    stage_meta = stage_event.metadata if stage_event is not None else {}
    metadata = {
        "previous_stage": from_stage or "",
        "previous_stage_label": _stage_label(from_stage),
        "new_stage": enrollment.stage or "",
        "new_stage_label": label,
        # What caused the change (explicit trigger, else the StageEvent's).
        "trigger": trigger or (stage_meta or {}).get("trigger", ""),
        "reason": note,
        "actor_label": (stage_meta or {}).get("actor_label", ""),
        "case_id": str(enrollment.case_id) if enrollment.case_id else "",
        # The GOVERNING internal-service case (may differ from the tied case) so
        # requested/disregarded rows record which case drove the action.
        "governing_case_id": _governing_case_id_for(enrollment),
        "program": enrollment.program_name or "",
        "kitchen": enrollment.kitchen.name if enrollment.kitchen_id else "",
        # Household roster at the time of the stage change (names of the members
        # on the request / disregard).
        "members": _enrollment_member_names(enrollment),
    }
    # On a carried verification, record BOTH governing cases (previous -> new) so
    # the history shows exactly which case switch carried the verification.
    if carried_verification:
        metadata["carried_from_case_id"] = carried_from_case
        metadata["carried_to_case_id"] = carried_to_case
    return emit_timeline_event(
        client=client,
        event_type=event_type,
        occurred_at=occurred,
        title=title,
        subtitle=subtitle,
        badge_text=label,
        badge_tone=tone,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        case=enrollment.case,
        metadata=metadata,
        dedupe_key=dedupe,
    )


def _format_address(address):
    region = " ".join(p for p in (address.state, address.zip) if p)
    unit = getattr(address, "unit", "")
    return ", ".join(
        p for p in (address.street, unit, address.city, region) if p
    )


def event_for_delivery_address_change(
    client, address, *, previous="", changes=None, enrollment=None,
    source=ChangeSource.CRM, actor="",
):
    """Emit a 'Delivery Address Changed' event. Not de-duped (each change is its
    own timeline point); ``previous`` is the pre-edit one-line address string.

    ``changes`` is the standardized :func:`build_change_list` output (a precise
    per-field diff, e.g. Street / Unit / ZIP / Delivery notes). When supplied it
    is used as-is -- so a notes-only edit still logs -- and the event is a no-op
    when it's empty. When omitted, a one-line address diff is derived (falls back
    to the legacy behaviour for callers that only pass ``previous``).
    """
    if client is None or address is None:
        return None
    new_addr = _format_address(address)
    if changes is None:
        changes = build_change_list([("Delivery address", previous, new_addr)])
    if not changes:
        return None  # nothing actually changed -- don't write a no-op row
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
        metadata={
            "previous": previous,
            "new": new_addr,
            "changes": changes,
        },
    )


def event_for_kitchen_assigned(
    enrollment, *, kitchen_name="", cadence_label="", source=ChangeSource.CRM, actor="",
):
    """Emit a 'Kitchen Assigned' event when a kitchen is ACTUALLY assigned to a
    household for the first time (the Logistics kitchen-assignment step). Distinct
    from reaching the Kitchen Assignment stage (AWAITING_KITCHEN), which only
    means the household is READY for assignment. Logged on the primary client."""
    client = getattr(enrollment, "client", None)
    if client is None:
        return None
    subtitle = (kitchen_name or "").strip()
    if cadence_label:
        subtitle = f"{subtitle} \u00b7 {cadence_label}" if subtitle else cadence_label
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.KITCHEN_ASSIGNED,
        occurred_at=timezone.now(),
        title="Kitchen Assigned",
        subtitle=subtitle,
        badge_text=(kitchen_name or "").strip() or "Assigned",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        case=getattr(enrollment, "case", None),
        metadata={
            "kitchen": (kitchen_name or "").strip(),
            "cadence": (cadence_label or "").strip(),
        },
    )


def event_for_kitchen_changed(
    enrollment, *, previous_kitchen="", new_kitchen="",
    previous_cadence="", new_cadence="", source=ChangeSource.CRM, actor="",
):
    """Emit a 'Kitchen Changed' event when a household's assigned kitchen and/or
    delivery cadence is changed (Logistics kitchen/cadence editors + the Kitchen
    Assignment pop-up re-assignment). Reuses the KITCHEN_ASSIGNED type. No-op
    when nothing actually changed, so callers can fire it unconditionally.

    Logged on the primary client; not de-duped so every change is preserved.
    """
    client = getattr(enrollment, "client", None)
    if client is None:
        return None
    changes = build_change_list([
        ("Kitchen", previous_kitchen, new_kitchen),
        ("Cadence", previous_cadence, new_cadence),
    ])
    if not changes:
        return None  # nothing actually changed -- don't write a no-op row
    # Lead the subtitle with whichever dimension changed (kitchen first).
    parts = [f"{c['field']}: {c['from']} \u2192 {c['to']}" for c in changes]
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.KITCHEN_ASSIGNED,
        occurred_at=timezone.now(),
        title="Kitchen Changed",
        subtitle=" \u00b7 ".join(parts),
        badge_text=(new_kitchen or "").strip() or "Changed",
        badge_tone=TimelineBadgeTone.INFO,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        case=getattr(enrollment, "case", None),
        metadata={
            "changes": changes,
            "previous_kitchen": (previous_kitchen or "").strip(),
            "new_kitchen": (new_kitchen or "").strip(),
            "previous_cadence": (previous_cadence or "").strip(),
            "new_cadence": (new_cadence or "").strip(),
        },
    )


def event_for_dietary_changed(profile, *, changes, enrollment=None, source=ChangeSource.CRM, actor=""):
    """Emit a 'Dietary Info Updated' event when an agent edits a member's dietary
    data (restrictions / allergies / menu type / meal category / notes).

    ``changes`` is the standardized :func:`build_change_list` output. No-op when
    empty. Logged on the MEMBER's own client so it shows on their history; not
    de-duped, so each edit is its own point.
    """
    client = getattr(profile, "client", None)
    if client is None or not changes:
        return None
    enrollment = enrollment or getattr(profile, "enrollment", None)
    fields = ", ".join(c["field"] for c in changes)
    name = profile.member_name or _client_full_name(client)
    subtitle = f"{name} \u00b7 {fields}" if name else fields
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.DIETARY_CHANGED,
        occurred_at=timezone.now(),
        title="Dietary Info Updated",
        subtitle=subtitle,
        badge_text="Updated",
        badge_tone=TimelineBadgeTone.INFO,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"changes": changes},
    )


def member_verification_snapshot(profile):
    """A point-in-time snapshot of one member's clinical/dietary verification data,
    stored on the 'Verification Submitted' event. The member's live
    ``MemberDietaryProfile`` can be edited later, but this preserves EXACTLY what
    was captured at verification (the audit record of what was verified)."""
    return {
        "member_name": (profile.member_name or "").strip(),
        "client_id": str(profile.client_id) if profile.client_id else "",
        "conditions": list(profile.conditions or []),          # Medical Conditions
        "medications": list(profile.medications or []),
        "weight": profile.weight or "",
        "height": profile.height or "",
        "on_medical_diet": bool(profile.on_medical_diet),
        "medical_diet_details": profile.medical_diet_details or "",
        "menu_type": profile.menu_type or "",
        "food_allergies": list(profile.food_allergies or []),
        "other_dietary_restrictions": profile.other_dietary_restrictions or "",
        "general_verification_notes": profile.general_verification_notes or "",
    }


def event_for_verification_submitted(
    enrollment, *, delivery_address="", delivery_weekdays=None,
    verified_flags=None, governing_case_id="", case_status="", auth_status="",
    source=ChangeSource.CRM, actor="",
):
    """Emit a 'Verification Completed' summary event capturing WHAT was verified:
    the governing case used (id + its status + authorization status AT SAVE TIME),
    the delivery address + days, the confirmed checkboxes, and a full per-member
    clinical/dietary snapshot (``member_verification_snapshot``) so the record
    preserves the verification even after the member's live profile changes.

    Reuses the VERIFICATION_COMPLETED type. De-duped per enrollment.
    Logged on the subject client's history.
    """
    client = getattr(enrollment, "client", None)
    if client is None:
        return None
    verified_flags = verified_flags or {}
    profiles = list(enrollment.member_profiles.all())
    members = [member_verification_snapshot(p) for p in profiles]
    n = len(members)
    subtitle = f"{n} member" + ("s" if n != 1 else "")
    if delivery_address:
        subtitle = f"{subtitle} \u00b7 {delivery_address}"
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.VERIFICATION_COMPLETED,
        occurred_at=timezone.now(),
        title="Verification Completed",
        subtitle=subtitle,
        badge_text="Completed",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        case=getattr(enrollment, "case", None),
        metadata={
            "governing_case_id": governing_case_id or _governing_case_id_for(enrollment),
            "case_status": case_status or "",
            "authorization_status": auth_status or "",
            "members": members,
            "delivery_address": delivery_address,
            "delivery_weekdays": list(delivery_weekdays or []),
            "verified": verified_flags,
        },
        dedupe_key=f"verification_submitted:{enrollment.pk}",
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
    # Surface WHY on the timeline row, not just who: "<name> — <reason>".
    name = profile.member_name or ""
    subtitle = f"{name} \u2014 {reason}".strip(" \u2014") if reason else name
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.OUT_OF_ORBIT,
        occurred_at=timezone.now(),
        title="Household set as Out of Orbit",
        subtitle=subtitle,
        badge_text="Out of Orbit",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"reason": reason, "menu_type": profile.menu_type or ""},
        dedupe_key=dedupe,
    )


def event_for_out_of_range(
    profile, *, enrollment=None, reason="", zip_code="", source=ChangeSource.SYSTEM, actor="",
):
    """Emit an 'Out of Range' event for a member whose delivery/primary ZIP is
    outside the service coverage area. Logged on the member's own client; de-duped
    per enrollment so re-running the coverage check doesn't duplicate the row."""
    client = getattr(profile, "client", None)
    if client is None:
        return None
    enrollment = enrollment or getattr(profile, "enrollment", None)
    dedupe = f"out_of_range:{enrollment.pk}:{profile.pk}" if enrollment is not None else ""
    # Surface WHY (reason / offending ZIP) on the row, not just who.
    name = profile.member_name or ""
    detail = reason or (f"ZIP {zip_code} outside coverage" if zip_code else "")
    subtitle = f"{name} \u2014 {detail}".strip(" \u2014") if detail else name
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.OUT_OF_RANGE,
        occurred_at=timezone.now(),
        title="Member set as Out of Range",
        subtitle=subtitle,
        badge_text="Out of Range",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"reason": reason, "zip": zip_code},
        dedupe_key=dedupe,
    )


def classify_ineligible_reason(reason):
    """Tag a human ineligibility ``reason`` string with a stable cause CODE so the
    History tab (and downstream filters) can tell WHY a member was made ineligible
    without parsing prose. Mirrors the reason strings produced in
    ``api.services.eligibility``:

    - ``medicaid_type``  -> Medicaid plan type not served (MLTC/MAP/FFS)
    - ``insurance``      -> no medical insurance / all plans expired
    - ``address``        -> out-of-range ZIP / unserved state (coverage area)
    - ``social_coverage``-> social-care-coverage gap (recoverable hold)
    - ``other``          -> anything else
    """
    r = (reason or "").lower()
    if "medicaid plan type" in r or "medicaid type" in r:
        return "medicaid_type"
    if "insurance" in r:
        return "insurance"
    if (
        "coverage area" in r or "out of range" in r or "not served" in r
        or "zip" in r or "state" in r
    ):
        return "address"
    if "social care coverage" in r:
        return "social_coverage"
    return "other"


def event_for_member_ineligible(
    client, *, reasons=None, source=ChangeSource.IMPORT, actor="",
):
    """Emit a 'Member marked Ineligible' event when the import-time eligibility
    check fails a CareCircle-unfixable gate (expired/missing insurance, wrong
    Medicaid type, out-of-range address). Logged on the member's own client. Not
    de-duped: it fires only on the transition INTO ineligible (the caller gates
    it), so a later recovery + re-flag records a fresh point.

    The metadata tags each reason with a stable CAUSE code (``causes`` +
    ``reason_causes``) so the history distinguishes an insurance-type off-ramp
    from an address/out-of-range one without re-parsing the reason text."""
    if client is None:
        return None
    reasons = [r for r in (reasons or []) if r]
    reason_causes = [
        {"reason": r, "cause": classify_ineligible_reason(r)} for r in reasons
    ]
    causes = sorted({rc["cause"] for rc in reason_causes})
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_INELIGIBLE,
        occurred_at=timezone.now(),
        title="Member marked Ineligible",
        subtitle="; ".join(reasons),
        badge_text="Ineligible",
        badge_tone=TimelineBadgeTone.DANGER,
        source=source,
        actor=actor,
        entity=client,
        metadata={"reasons": reasons, "causes": causes, "reason_causes": reason_causes},
    )


def event_for_member_eligibility_restored(
    client, *, source=ChangeSource.IMPORT, actor="",
):
    """Emit an 'Eligibility restored' event when a previously-ineligible member
    passes the eligibility checks again on a later import."""
    if client is None:
        return None
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_ELIGIBILITY_RESTORED,
        occurred_at=timezone.now(),
        title="Member eligibility restored",
        badge_text="Eligible",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=client,
    )


def event_for_member_coverage_hold(
    client, *, reasons=None, source=ChangeSource.IMPORT, actor="",
):
    """Emit a 'Coverage Hold' event when the import-time eligibility check finds a
    RECOVERABLE social-care-coverage gap (expired/missing coverage): service is
    paused rather than the hard INELIGIBLE off-ramp. Fires only on the transition
    INTO the hold (the caller gates it)."""
    if client is None:
        return None
    reasons = [r for r in (reasons or []) if r]
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_COVERAGE_HOLD,
        occurred_at=timezone.now(),
        title="Service paused \u2014 coverage hold",
        subtitle="; ".join(reasons),
        badge_text="On Hold",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=client,
        metadata={"reasons": reasons},
    )


def event_for_member_coverage_restored(
    client, *, source=ChangeSource.IMPORT, actor="",
):
    """Emit a 'Coverage Restored' event when a member's social-care coverage is
    renewed on a later import, lifting the recoverable coverage hold."""
    if client is None:
        return None
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_COVERAGE_RESTORED,
        occurred_at=timezone.now(),
        title="Service resumed \u2014 coverage restored",
        badge_text="Resumed",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=client,
    )


def event_for_member_service_inactive(
    client, *, case_id=None, program="", closed_on="", source=ChangeSource.IMPORT,
    actor="",
):
    """Emit a 'Service Inactive' event when a member's LAST open internal-service
    (meal/box) case closes: no open case remains, so service is paused. Reversible
    -- a new open case emits the matching reactivation event. Fires only on the
    transition INTO inactive (the caller gates it)."""
    if client is None:
        return None
    parts = [p for p in (f"case {case_id}" if case_id else "", program) if p]
    subtitle = " \u00b7 ".join(parts)
    if closed_on:
        subtitle = f"{subtitle} closed {closed_on}" if subtitle else f"Closed {closed_on}"
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_SERVICE_INACTIVE,
        occurred_at=timezone.now(),
        title="Service Inactive",
        subtitle=subtitle or "No open internal-service case remains",
        badge_text="Inactive",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=client,
        metadata={
            "case_id": str(case_id) if case_id else "",
            "program": program or "",
            "closed_on": closed_on or "",
        },
    )


def event_for_member_service_reactivated(
    client, *, source=ChangeSource.IMPORT, actor="",
):
    """Emit a 'Service Reactivated' event when a previously SERVICE_INACTIVE
    member gets a new OPEN internal-service case, reopening service."""
    if client is None:
        return None
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_SERVICE_REACTIVATED,
        occurred_at=timezone.now(),
        title="Service Reactivated",
        badge_text="Reactivated",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=client,
    )


def event_for_member_governing_case_changed(
    client, *, previous_case_id="", new_case_id="", auth_status="", program="",
    reason="", source=ChangeSource.IMPORT, actor="",
):
    """Emit a 'Governing Case Changed' event when the internal-service case whose
    authorization GOVERNS a member's program changes (a newer case was approved
    and superseded the prior one, or the prior governing case closed). Recorded
    once per actual old->new transition: the ``dedupe_key`` keys the event on the
    exact ``previous -> new`` pair, so re-running the case reconcile against an
    unchanged governing case never duplicates it. The caller (``lifecycle``) also
    guards on ``Client.governing_internal_case_id`` so the FIRST governing case
    to land is recorded silently (there is no prior case to switch from)."""
    if client is None or not new_case_id or not previous_case_id:
        return None
    prog = (program or "").strip() or "meal/box"
    auth = (auth_status or "").strip() or "blank"
    # Badge is a SHORT product-kind label (Boxes/Meals): the raw program name can
    # exceed the badge_text column (max_length=120) and a DataError here aborts the
    # surrounding case-save transaction (a 500 on every case save that changes the
    # governing case), so never store the full name in the badge.
    prog_low = prog.lower()
    badge = "Boxes" if "box" in prog_low else "Meals" if "meal" in prog_low else "Program"
    subtitle = f"{previous_case_id} \u2192 {new_case_id} \u00b7 {prog}"
    if reason:
        subtitle = f"{subtitle} \u00b7 {reason}"
    # Defensively fit the display columns (subtitle max_length=255).
    subtitle = subtitle[:255]
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED,
        occurred_at=timezone.now(),
        title="Governing Case Changed",
        subtitle=subtitle,
        badge_text=badge[:120],
        badge_tone=TimelineBadgeTone.INFO,
        source=source,
        actor=actor,
        entity=client,
        metadata={
            "previous_case_id": str(previous_case_id),
            "new_case_id": str(new_case_id),
            "authorization_status": auth_status or "",
            "program": program or "",
            "reason": reason or "",
        },
        dedupe_key=(
            f"governing_case_changed:{client.pk}:{previous_case_id}:{new_case_id}"
        ),
    )


def event_for_member_program_switched(
    client, *, previous_kind="", new_kind="", previous_case_id="",
    new_case_id="", auth_status="", reason="", source=ChangeSource.IMPORT,
    actor="",
):
    """Emit a 'Program Switched' event when the household's GOVERNING internal-
    service case switches product KIND (meals<->boxes) to an authorized case: the
    household's future deliveries were stopped and it was requeued for a NEW
    kitchen assignment (a new kitchen + cadence + delivery calendar) under the new
    product. De-duped on the exact ``previous -> new`` case pair, so re-running the
    case reconcile against an unchanged governing case never duplicates it."""
    if client is None or not new_kind:
        return None
    prev = (previous_kind or "").strip() or "\u2014"
    new = (new_kind or "").strip()
    subtitle = f"{prev} \u2192 {new}"
    if reason:
        subtitle = f"{subtitle} \u00b7 {reason}"
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_PROGRAM_SWITCHED,
        occurred_at=timezone.now(),
        title="Program Switched",
        subtitle=subtitle,
        badge_text=new,
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=client,
        metadata={
            "previous_kind": previous_kind or "",
            "new_kind": new_kind or "",
            "previous_case_id": str(previous_case_id) if previous_case_id else "",
            "new_case_id": str(new_case_id) if new_case_id else "",
            "authorization_status": auth_status or "",
            "reason": reason or "",
        },
        dedupe_key=(
            f"program_switched:{client.pk}:{previous_case_id}:{new_case_id}"
        ),
    )


def event_for_member_case_mismatch(
    client, *, mismatch_type="", previous_case_id="", new_case_id="",
    previous_household_type="", new_household_type="", reason="",
    source=ChangeSource.IMPORT, actor="", auto_resolved=False,
):
    """Emit a household SCOPE-switch (household<->individual) event on the
    GOVERNING internal-service case.

    When ``auto_resolved`` is True (the import-driven auto-reconcile) the switch
    was APPLIED automatically -- no Customer Service action is required -- so it
    renders as an informational 'Case Scope Reconciled' audit row. Otherwise it
    is the legacy 'Case Mismatch' that needs CS review. De-duped on the exact
    ``previous -> new`` case pair so a re-import never duplicates it."""
    if client is None or not new_case_id or not previous_case_id:
        return None
    prev = (previous_household_type or "").strip() or "\u2014"
    new = (new_household_type or "").strip() or "\u2014"
    subtitle = f"{prev.title()} \u2192 {new.title()}"
    if reason:
        subtitle = f"{subtitle} \u00b7 {reason}"
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_CASE_MISMATCH,
        occurred_at=timezone.now(),
        title="Case Scope Reconciled" if auto_resolved else "Case Mismatch",
        subtitle=subtitle,
        badge_text="Auto-Reconciled" if auto_resolved else "Needs CS Review",
        badge_tone=(
            TimelineBadgeTone.INFO if auto_resolved else TimelineBadgeTone.WARNING
        ),
        source=source,
        actor=actor,
        entity=client,
        metadata={
            "mismatch_type": mismatch_type or "",
            "previous_case_id": str(previous_case_id),
            "new_case_id": str(new_case_id),
            "previous_household_type": previous_household_type or "",
            "new_household_type": new_household_type or "",
            "reason": reason or "",
            "auto_resolved": bool(auto_resolved),
        },
        dedupe_key=(
            f"case_mismatch:{client.pk}:{previous_case_id}:{new_case_id}"
        ),
    )


def event_for_household_member_added(
    primary_client, member_client, *, enrollment=None, source=ChangeSource.CRM,
    actor="", added_from="",
):
    """Emit a 'Household Member Added' event on the PRIMARY client's timeline
    when an agent adds another member to the household (e.g. via the
    verification wizard's member search). De-duped per primary+member so
    re-saving the verification doesn't duplicate the row.

    ``added_from`` is a human-readable origin (e.g. "the Household tab", "the
    verification pop-up") appended to the description so it's clear WHERE the
    member was added.
    """
    if primary_client is None or member_client is None:
        return None
    name = f"{member_client.first_name} {member_client.last_name}".strip() or "New member"
    subtitle = f"{name} · added from {added_from}" if added_from else name
    return emit_timeline_event(
        client=primary_client,
        event_type=TimelineEventType.HOUSEHOLD_MEMBER_ADDED,
        occurred_at=timezone.now(),
        title="Household Member Added",
        subtitle=subtitle,
        badge_text="Added",
        badge_tone=TimelineBadgeTone.INFO,
        source=source,
        actor=actor,
        entity=member_client,
        enrollment=enrollment,
        metadata={
            "member_client_id": str(member_client.pk),
            "added_from": added_from,
            "governing_case_id": _governing_case_id_for(enrollment),
        },
        dedupe_key=f"household_member_added:{primary_client.pk}:{member_client.pk}",
    )


def event_for_household_member_removed(
    primary_client, member_client, *, member_name="", enrollment=None,
    source=ChangeSource.CRM, actor="", removed_from="",
):
    """Emit a 'Household Member Removed' event on the PRIMARY client's timeline
    when an agent removes a member from the household.

    ``removed_from`` is a human-readable origin (e.g. "the Household tab")
    appended to the description so it's clear WHERE the member was removed.
    Not de-duped: each removal is a distinct point on the timeline (a member can
    be removed, re-added and removed again).
    """
    if primary_client is None:
        return None
    name = member_name
    if not name and member_client is not None:
        name = f"{member_client.first_name} {member_client.last_name}".strip()
    name = name or "Member"
    subtitle = f"{name} · removed from {removed_from}" if removed_from else name
    return emit_timeline_event(
        client=primary_client,
        event_type=TimelineEventType.HOUSEHOLD_MEMBER_REMOVED,
        occurred_at=timezone.now(),
        title="Household Member Removed",
        subtitle=subtitle,
        badge_text="Removed",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=member_client,
        enrollment=enrollment,
        metadata={
            "member_client_id": str(member_client.pk) if member_client else "",
            "removed_from": removed_from,
            "governing_case_id": _governing_case_id_for(enrollment),
        },
    )


def event_for_product_type_changed(
    enrollment, *, previous_label="", new_label="", source=ChangeSource.CRM,
    actor="", dedupe_key="",
):
    """Emit a 'Product Type Changed' event when a household's meals/boxes product
    changes -- both an agent's Household-tab correction and the system's
    governing-case product switch (meals<->boxes). Logged on the primary client.

    ``dedupe_key`` (e.g. the case pair on a governing-case switch) makes the write
    create-once for that switch; left blank (manual corrections) every change is
    recorded."""
    client = getattr(enrollment, "client", None)
    if client is None:
        return None
    prev = (previous_label or "").strip()
    new = (new_label or "").strip() or "—"
    subtitle = f"{prev} \u2192 {new}" if prev else new
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.PRODUCT_TYPE_CHANGED,
        occurred_at=timezone.now(),
        title="Product Type Changed",
        subtitle=subtitle,
        badge_text=new,
        badge_tone=TimelineBadgeTone.INFO,
        source=source,
        actor=actor,
        entity=enrollment,
        enrollment=enrollment,
        metadata={"previous": prev, "new": new},
        dedupe_key=dedupe_key,
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


def event_for_member_paused(
    profile, *, enrollment=None, reason="", source=ChangeSource.ADMIN, actor="",
):
    """Emit a 'Member Paused' event when an agent manually pauses an individual
    member (with a required reason). Like Out of Orbit, paused members are
    excluded from delivery schedules / Purchase Orders until unpaused. Logged on
    the member's own client. Not de-duped, so each pause/unpause cycle is
    recorded."""
    client = getattr(profile, "client", None)
    if client is None:
        return None
    enrollment = enrollment or getattr(profile, "enrollment", None)
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_PAUSED,
        occurred_at=timezone.now(),
        title="Member Paused",
        subtitle=profile.member_name or "",
        badge_text="Paused",
        badge_tone=TimelineBadgeTone.WARNING,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"reason": reason, "menu_type": profile.menu_type or ""},
    )


def event_for_member_unpaused(
    profile, *, enrollment=None, reason="", source=ChangeSource.ADMIN, actor="",
):
    """Emit a 'Member Unpaused' event when an agent lifts a manual pause. The
    caller re-runs the meal rule first, so the member may land back Active or
    Out of Orbit; this records only that the pause was lifted. Logged on the
    member's own client. Not de-duped."""
    client = getattr(profile, "client", None)
    if client is None:
        return None
    enrollment = enrollment or getattr(profile, "enrollment", None)
    return emit_timeline_event(
        client=client,
        event_type=TimelineEventType.MEMBER_UNPAUSED,
        occurred_at=timezone.now(),
        title="Member Unpaused",
        subtitle=profile.member_name or "",
        badge_text="Unpaused",
        badge_tone=TimelineBadgeTone.SUCCESS,
        source=source,
        actor=actor,
        entity=profile,
        enrollment=enrollment,
        metadata={"reason": reason, "menu_type": profile.menu_type or ""},
    )

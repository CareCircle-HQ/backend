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


def _eligibility_stage(client):
    """Return ELIGIBLE / INELIGIBLE / None from the client's eligibility results.

    Prefers ELIGIBLE when results are mixed. Returns None when no eligibility
    result has been resolved yet.
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
        return ClientStage.ELIGIBLE
    if any(is_ineligible(s) for s in statuses):
        return ClientStage.INELIGIBLE
    return None


def derive_client_stage(client):
    """Compute the funnel stage from the client's current data (no writes).

    Priority (highest first): client > eligible|ineligible > screened >
    prospect > lead.
    """
    if _has_met_council_case(client):
        return ClientStage.CLIENT

    elig = _eligibility_stage(client)
    if elig is not None:
        return elig

    if _has_met_council_screening(client):
        return ClientStage.SCREENED

    if (client.consent_status or "").lower() == "accepted":
        return ClientStage.PROSPECT

    return ClientStage.LEAD


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
    return enrollment

"""Dispatch: work sent to external vendors, and the evidence that comes back.

The rules this enforces, and where each comes from
(`docs/housing-assessment-order-plan.md`):

* ONE assessment order per member; remediation orders hang off it.
* An order cannot be SUBMITTED without a vendor signature, a member signature, and
  at least one photo PER FINDING.
* A submitted order is LOCKED. CRM users may never edit vendor evidence; the vendor
  corrects a mistake by VOIDING their submission and making a new one, and the
  voided copy is kept.
* Remediation cases that arrive before the assessment order exists are ADOPTED when
  it is created -- not rebuilt. Nothing here recomputes or rewrites; the food
  side's rebuild/replace machinery is what forked 149 enrollments.
"""
import logging

from django.db import transaction
from django.utils import timezone

from api.models import (
    DispatchKind, DispatchOrder, DispatchSignerRole, DispatchStatus,
    DispatchSubmission, DispatchSubmissionState, StageEntityType, StageEvent,
    StageEventSource,
)

logger = logging.getLogger(__name__)

# The linear chain. Not independent flags: each status has exactly one successor,
# which is what makes "what happens next?" answerable from the data alone.
STATUS_ORDER = [
    DispatchStatus.PENDING_SCHEDULE,
    DispatchStatus.CONFIRMED,
    DispatchStatus.PENDING_SUBMISSION,
    DispatchStatus.SUBMITTED,
    DispatchStatus.UPLOADED,
]

# Minimum DISTINCT DATES of member availability the wizard must collect. Counted as
# dates, not rows -- three windows on one Tuesday does not satisfy it.
MIN_AVAILABILITY_DATES = 3


def record_transition(order, from_status, to_status, *, actor=None, source=None,
                      note="", metadata=None):
    """Append the transition to StageEvent -- the same log the food stages use.

    Deliberately the shared log rather than a dispatch-only table, so "everything
    that happened to this member" stays one query.
    """
    # StageEvent.actor is a FK to the Django auth User, but portal callers hand us
    # the DRF AgentUser principal or an Agent row. Assigning either raises
    # ValueError and, per stage_event_actor's own docstring, "aborts the stage
    # change (and, via reconcile, the whole case upsert)". Coerce HERE rather than
    # at each call site, so no future caller can reintroduce it.
    from api.services.lifecycle import stage_event_actor

    return StageEvent.objects.create(
        entity_type=StageEntityType.DISPATCH_ORDER,
        client=order.client,
        dispatch_order=order,
        from_stage=from_status or "",
        to_stage=to_status,
        source=source or StageEventSource.MANUAL,
        actor=stage_event_actor(actor),
        note=note,
        metadata=metadata or {},
    )


def set_status(order, to_status, *, actor=None, source=None, note="", metadata=None):
    """Move an order and record it. No-op when already there."""
    from_status = order.status
    if from_status == to_status:
        return None
    order.status = to_status
    order.save(update_fields=["status", "updated_at"])
    return record_transition(
        order, from_status, to_status,
        actor=actor, source=source, note=note, metadata=metadata,
    )


# ── the submission gate ──────────────────────────────────────────────────────

def missing_for_submission(order):
    """What still blocks SUBMITTED. Empty list means the gate is satisfied.

    Returns REASONS rather than a bool on purpose: the vendor has to be told what
    is missing, and a bare False at the end of a home visit is useless. The API and
    any UI both read this, so the rule lives in exactly one place -- a UI-only
    check is how the food verification endpoint still accepted a housing case after
    the picker had been fixed.
    """
    missing = []

    submission = active_submission(order)
    roles = set()
    if submission is not None:
        roles = set(
            submission.signatures.values_list("signer_role", flat=True)
        )
    if DispatchSignerRole.VENDOR not in roles:
        missing.append("vendor signature")
    if DispatchSignerRole.MEMBER not in roles:
        missing.append("member signature")

    # At least one photo PER FINDING -- which is why DispatchProof carries a
    # finding FK. Proofs with no finding are general site photos and do not count
    # toward any finding's requirement.
    for finding in order.findings.all():
        if not finding.proofs.exists():
            missing.append(f"photo for finding: {finding.title}")

    return missing


def can_submit(order):
    return not missing_for_submission(order)


# ── submissions: create, submit, void ────────────────────────────────────────

def active_submission(order):
    """The one live submission, or None. Voided ones are history."""
    return order.submissions.filter(
        state=DispatchSubmissionState.ACTIVE,
    ).first()


@transaction.atomic
def open_submission(order):
    """Start (or return) the live submission the vendor is filling in.

    Idempotent: an offline client that retries must not create two.
    """
    existing = active_submission(order)
    if existing is not None:
        return existing
    last = order.submissions.order_by("-sequence").first()
    return DispatchSubmission.objects.create(
        dispatch_order=order,
        sequence=(last.sequence + 1) if last else 1,
    )


@transaction.atomic
def submit(order, *, vendor_user=None, note=""):
    """Submit the active submission. Raises ValueError listing what is missing.

    The gate is checked HERE, server-side, not in the caller.
    """
    missing = missing_for_submission(order)
    if missing:
        raise ValueError(f"cannot submit: missing {', '.join(missing)}")

    submission = active_submission(order)
    if submission is None:
        raise ValueError("cannot submit: no active submission")

    submission.submitted_at = timezone.now()
    submission.submitted_by = vendor_user
    submission.save(update_fields=["submitted_at", "submitted_by"])
    set_status(
        order, DispatchStatus.SUBMITTED,
        source=StageEventSource.AUTO, note=note,
        metadata={"submission": str(submission.dispatch_submission_id)},
    )
    return submission


@transaction.atomic
def void_submission(submission, *, vendor_user=None, reason=""):
    """Void a submitted submission so the vendor can correct and resubmit.

    Keeps the voided copy entire -- its signatures and its PDF hash are the record
    of what was attested at the time, and are what make the correction auditable
    rather than a rewrite.

    Returns the NEW active submission, ready to be filled in.
    """
    if submission.state == DispatchSubmissionState.VOIDED:
        raise ValueError("submission is already voided")

    order = submission.dispatch_order
    submission.state = DispatchSubmissionState.VOIDED
    submission.voided_at = timezone.now()
    submission.voided_by = vendor_user
    submission.void_reason = reason
    submission.save(
        update_fields=["state", "voided_at", "voided_by", "void_reason"]
    )

    # Unite Us may already hold this evidence. Flag those uploads as superseded so
    # the drift is VISIBLE -- otherwise Unite Us keeps a document the CRM now
    # rejects and nothing says so, which is the silent-divergence class of bug.
    superseded = submission.uniteus_uploads.filter(superseded_at=None).update(
        superseded_at=timezone.now()
    )

    record_transition(
        order, order.status, order.status,
        actor=None, source=StageEventSource.MANUAL,
        note=reason or "submission voided",
        metadata={
            "voided_submission": str(submission.dispatch_submission_id),
            "sequence": submission.sequence,
            "superseded_uploads": superseded,
        },
    )
    # The order is awaiting a valid submission again; the gate must be met afresh.
    set_status(
        order, DispatchStatus.PENDING_SUBMISSION,
        source=StageEventSource.AUTO, note="voided, awaiting resubmission",
    )
    return open_submission(order)


# ── linking remediation cases ────────────────────────────────────────────────

def assessment_order_for(client):
    """The member's assessment order, or None."""
    return DispatchOrder.objects.filter(
        client=client, kind=DispatchKind.ASSESSMENT,
    ).first()


@transaction.atomic
def create_remediation_order(case, *, assessment=None):
    """Create the remediation order for a Home Remediation case.

    Returns None when the member has no assessment order yet -- the case simply
    WAITS and is adopted when the assessment is created. Inventing an assessment
    they never had would fabricate history.

    Idempotent via the (case, kind=remediation) unique constraint.
    """
    assessment = assessment or assessment_order_for(case.client)
    if assessment is None:
        return None
    existing = DispatchOrder.objects.filter(
        case=case, kind=DispatchKind.REMEDIATION,
    ).first()
    if existing is not None:
        return existing

    from api.models import CaseStatus

    closed = (case.case_status or "").lower() in ("closed", "cancelled")
    # What the vendor is being asked to fit, and where. Captured NOW, from the
    # program name, so the instruction survives a later program rename.
    from api.services.housing import housing_work_order_item

    item, location = housing_work_order_item(case)
    order = DispatchOrder.objects.create(
        kind=DispatchKind.REMEDIATION,
        parent=assessment,
        client=case.client,
        case=case,
        vendor=assessment.vendor,
        item=item,
        location=location,
        # A closed case is adopted as RECORD ONLY. Putting it in a schedulable
        # status would dispatch a vendor to work nobody is paying for.
        status=(
            DispatchStatus.CANCELLED if closed else DispatchStatus.PENDING_SCHEDULE
        ),
    )
    record_transition(
        order, "", order.status,
        source=StageEventSource.AUTO,
        note=(
            f"work order created: {item} ({location})" if item
            else "work order created from housing case"
        ),
        metadata={
            "case_id": str(case.case_id), "adopted_closed": closed,
            "item": item, "location": location,
            "program_name": case.program_name or "",
        },
    )
    return order


@transaction.atomic
def adopt_unlinked_remediation_cases(assessment):
    """Attach the member's existing Home Remediation cases to their new assessment.

    Called when an assessment order is created. Cases that arrived BEFORE it --
    MIRIAM ISRAEL holds nine, five open and four closed -- have been waiting with
    no order; this gives them one.

    Called ADOPTION, not rebuild: nothing is recomputed and the cases themselves
    are untouched. They simply gain an order and a parent.
    """
    from api.services.housing import housing_work_orders

    adopted = []
    for case in housing_work_orders(assessment.client):
        order = create_remediation_order(case, assessment=assessment)
        if order is not None:
            adopted.append(order)
    return adopted


@transaction.atomic
def reconcile_dispatch_orders(client):
    """Bring a member's dispatch orders in line with their housing cases.

    Called ONCE per client after an import, from the same places as
    ``reconcile_internal_service_authorization`` -- so it sees the COMPLETE case
    picture rather than firing per row against a partial one. A member can arrive
    carrying several Home Remediation cases in one payload.

    Only ever CREATES: a remediation order for each Home Remediation case, once the
    member has an assessment order. Cases that arrive first simply wait, and are
    picked up here on a later import or by adoption when the assessment is created.

    Never raises -- a dispatch hiccup must not fail a case import, the same
    contract the food reconcile already honours.
    """
    try:
        assessment = assessment_order_for(client)
        if assessment is None:
            return []
        return adopt_unlinked_remediation_cases(assessment)
    except Exception:  # noqa: BLE001 - never fail the import
        logger.exception("reconcile_dispatch_orders failed for client %s", client.pk)
        return []

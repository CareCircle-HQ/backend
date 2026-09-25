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
                      note="", metadata=None, agent=None):
    """Append the transition to StageEvent -- the same log the food stages use.

    Deliberately the shared log rather than a dispatch-only table, so "everything
    that happened to this member" stays one query.

    ``agent`` is written into the metadata as a NAME and CODE. That is not
    redundant with ``actor``: StageEvent.actor is a FK to the auth User, portal
    callers only ever have the DRF AgentUser principal, and stage_event_actor
    coerces it to None -- so without this the History tab would show every
    agent-driven change as having no user. Storing the name rather than only an id
    also means the history still reads correctly if an agent record is later
    renamed or deactivated.
    """
    # StageEvent.actor is a FK to the Django auth User, but portal callers hand us
    # the DRF AgentUser principal or an Agent row. Assigning either raises
    # ValueError and, per stage_event_actor's own docstring, "aborts the stage
    # change (and, via reconcile, the whole case upsert)". Coerce HERE rather than
    # at each call site, so no future caller can reintroduce it.
    from api.services.lifecycle import stage_event_actor

    meta = dict(metadata or {})
    if agent is not None:
        meta.setdefault("agent_name", getattr(agent, "name", "") or "")
        # agent_code is NULLABLE in real data -- "Alexis Tamayo" has none -- so the
        # id is stored as well. The name is what gets displayed; the id is what
        # still identifies the actor if a name is later changed.
        meta.setdefault("agent_code", getattr(agent, "agent_code", "") or "")
        meta.setdefault("agent_id", str(getattr(agent, "pk", "") or ""))

    return StageEvent.objects.create(
        entity_type=StageEntityType.DISPATCH_ORDER,
        client=order.client,
        dispatch_order=order,
        from_stage=from_status or "",
        to_stage=to_status,
        source=source or StageEventSource.MANUAL,
        actor=stage_event_actor(actor),
        note=note,
        metadata=meta,
    )


def set_status(order, to_status, *, actor=None, source=None, note="", metadata=None,
               agent=None):
    """Move an order and record it. No-op when already there."""
    from_status = order.status
    if from_status == to_status:
        return None
    order.status = to_status
    order.save(update_fields=["status", "updated_at"])
    return record_transition(
        order, from_status, to_status,
        actor=actor, source=source, note=note, metadata=metadata, agent=agent,
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

    # THE SPEND CAP. Unite Us authorises one amount for the whole service, so a
    # recommendation above it cannot be billed -- refusing at submit is the last
    # point at which it is cheap to fix, because the vendor is still on site.
    #
    # The message carries NO amount: the vendor app shows this to a member.
    from .pricing import cap_status

    questionnaire = getattr(order, "questionnaire", None)
    if questionnaire is not None and questionnaire.interventions:
        if cap_status(order.vendor, questionnaire.interventions)["over_cap"]:
            missing.append(
                "the recommended items exceed the funding limit for this service"
            )

    # A PHOTO PER IDENTIFIED PROBLEM, and at least one of the dwelling.
    #
    # Per CATEGORY, not per question: three questions point at grab bars, and a
    # photo each would ask for the same photo three times. The problem evidenced is
    # "no grab bars", however many questions surfaced it.
    #
    # This is stricter than the form's printed minimum ("at least one photo of the
    # dwelling"), which it subsumes -- a category photo IS a photo of the dwelling.
    # It is NOT the old per-FINDING rule that shipped here and that every real
    # submission would have failed; findings are a different concept, and a photo
    # per item belongs to work orders as proof of service.
    from .assessment_forms import photo_groups_required

    questionnaire = getattr(order, "questionnaire", None)
    needed = (
        photo_groups_required(questionnaire.answers, questionnaire.modules)
        if questionnaire is not None else []
    )

    if needed:
        have = set(
            order.proofs.exclude(intervention_group="")
            .values_list("intervention_group", flat=True)
        )
        for code, label in needed:
            if code not in have:
                missing.append(f"photo of: {label}")
    elif not order.proofs.exists():
        # Nothing that needs evidencing was ticked -- the printed minimum still
        # applies, so a visit that found no problems is still evidenced.
        missing.append("at least one photo of the dwelling")

    return missing


def _intervention_group_labels():
    """Product group code -> human label. Used when naming a product, not the gate.

    The gate now names the QUESTION group a photo is owed for ("photo of:
    Bathroom"), because that is the heading the vendor just answered under -- the
    questionnaires attach the photo requirement to the question group, not to a
    product category.
    """
    from .assessment_forms import INTERVENTIONS

    return {
        g["code"]: g["label"]
        for groups in INTERVENTIONS.values() for g in groups
    }


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
    # REOPEN THE QUESTIONNAIRE. Without this the void was only half done: the
    # order went back to PENDING_SUBMISSION while the form stayed SUBMITTED, so the
    # vendor's draft endpoint answered 409 and "void so the vendor can correct and
    # resubmit" was impossible to actually do.
    #
    # The ANSWERS are kept -- a correction is an edit, and retyping 33 questions to
    # fix one is how a vendor ends up ticking from memory. What is cleared is
    # submitted_at, because the form is no longer submitted.
    from ..models import DispatchQuestionnaireState

    questionnaire = getattr(order, "questionnaire", None)
    if questionnaire is not None and questionnaire.is_submitted:
        questionnaire.state = DispatchQuestionnaireState.DRAFT
        questionnaire.submitted_at = None
        questionnaire.save(update_fields=["state", "submitted_at", "updated_at"])

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
def sync_dispatch_items(assessment):
    """Create a DispatchItem for each of the member's housing work-order cases.

    Replaces the old one-order-per-case model. A case now yields an ITEM -- "a grab
    bar in Brooklyn" -- and a work order is a batch an agent later assembles from
    approved items. So this runs on both entry points and only ever ADDS:

      * when the assessment order is created, for cases that arrived first;
      * on every import, for cases that arrive afterwards.

    Idempotent through the one-item-per-case constraint, and it never touches an
    item that is already in a work order -- re-importing a case must not re-describe
    work a vendor has already been sent.
    """
    from api.models import DispatchItem
    from api.services.housing import housing_work_order_item, housing_work_orders

    created = []
    existing = set(
        DispatchItem.objects
        .filter(case__client=assessment.client)
        .values_list("case_id", flat=True)
    )
    for case in housing_work_orders(assessment.client):
        if case.case_id in existing:
            continue
        item, location = housing_work_order_item(case)
        row = DispatchItem.objects.create(
            assessment=assessment, case=case,
            item=item, location=location, program_name=case.program_name or "",
        )
        created.append(row)
        record_transition(
            assessment, assessment.status, assessment.status,
            source=StageEventSource.AUTO,
            note=(f"item added: {item} ({location})" if item else "housing item added"),
            metadata={
                "case_id": str(case.case_id), "item": item, "location": location,
                "program_name": case.program_name or "",
            },
        )
    return created


# Kept under the old name because the assessment-order endpoint and the import
# reconcile both call it, and "adopt" is still what it does -- it now adopts the
# member's waiting cases as ITEMS rather than as orders.
def adopt_unlinked_remediation_cases(assessment):
    return sync_dispatch_items(assessment)


@transaction.atomic
def create_work_order(assessment, item_ids, *, vendor=None, actor=None, notes="",
                      agent=None, allow_expired=False):
    """Assemble a work order from approved, undispatched items.

    Raises ValueError naming the offending items rather than silently dropping
    them: an agent who selected six and got a work order for four would not notice,
    and the two that vanished are exactly the ones someone is waiting on.

    Every item must be AVAILABLE -- approved, its authorization window still open,
    and not already dispatched. Checked here rather than trusted from the request,
    because the selection list an agent saw may be seconds stale -- and an
    authorization window can lapse between loading the page and submitting it.

    ``allow_expired`` admits items blocked SOLELY by a lapsed authorization window.

    ⚠ A DELIBERATE OVERRIDE, NOT A RELAXATION. Unite Us routinely leaves a Home
    Remediation case open past its window, and the work still has to happen -- MIRIAM
    ISRAEL had 9 approved devices, every window ended 2026-09-18, and the panel
    offered no way to dispatch any of them. Installing against a lapsed window is a
    BILLING RISK, so it is opt-in per request and named in the order's notes rather
    than done quietly.

    It never admits an UNAPPROVED item (unfunded) or an ALREADY-DISPATCHED one
    (double-dispatch). Those are not judgement calls.
    """
    from api.models import DispatchItem, DispatchKind, DispatchOrder

    items = list(
        DispatchItem.objects
        .select_related("case")
        .filter(dispatch_item_id__in=item_ids, assessment=assessment)
    )
    if not items:
        raise ValueError("select at least one item")

    missing = set(str(i) for i in item_ids) - {
        str(i.dispatch_item_id) for i in items
    }
    if missing:
        raise ValueError(f"unknown item(s): {', '.join(sorted(missing))}")

    # The reason lives on the model, so this error, the serializer and the panel
    # cannot describe the same item three different ways.
    unavailable = [
        i for i in items
        if not i.is_available and not (allow_expired and i.expired_only)
    ]
    if unavailable:
        why = ", ".join(
            f"{i.item or i.case_id} ({i.unavailable_reason})" for i in unavailable
        )
        raise ValueError(f"cannot dispatch: {why}")

    # Which ones went out on an expired authorization. Recorded, because the whole
    # justification for allowing the override is that it is visible afterwards.
    overridden = [i for i in items if i.expired_only] if allow_expired else []

    # ⚠ THE OVERRIDE GOES IN THE NOTES A HUMAN READS, not only the audit metadata.
    # Recorded in metadata alone it would be discoverable but not visible, and an
    # expired-authorization dispatch is a billing conversation waiting to happen.
    order_notes = notes
    if overridden:
        detail = ", ".join(
            f"{i.item} (ended {i.authorization_window[1]:%b %-d, %Y})"
            for i in overridden
        )
        line = f"Dispatched on an EXPIRED authorization: {detail}."
        order_notes = f"{notes}\n\n{line}" if notes else line

    # One borough per work order: a vendor visit is a trip to an address, and the
    # items all belong to the same dwelling anyway. Mixed boroughs would mean the
    # parse went wrong somewhere.
    locations = {i.location for i in items if i.location}
    order = DispatchOrder.objects.create(
        kind=DispatchKind.REMEDIATION,
        parent=assessment,
        client=assessment.client,
        vendor=vendor or assessment.vendor,
        location=locations.pop() if len(locations) == 1 else "",
        status=DispatchStatus.PENDING_SCHEDULE,
        notes=order_notes,
        # Inherited, not copied: the address was verified once on the assessment.
    )
    DispatchItem.objects.filter(
        dispatch_item_id__in=[i.dispatch_item_id for i in items],
    ).update(dispatch_order=order)
    # The vendor's tentative install windows, copied so this order can be
    # rescheduled without moving any other order from the same assessment.
    copy_install_windows(assessment, order)

    record_transition(
        order, "", order.status,
        actor=actor, source=StageEventSource.MANUAL, agent=agent,
        note=f"work order created with {len(items)} item(s)",
        metadata={
            "items": [i.item for i in items],
            "item_ids": [str(i.dispatch_item_id) for i in items],
            "case_ids": [str(i.case_id) for i in items],
            # Empty on a normal dispatch, so its presence IS the flag.
            "expired_override": [str(i.dispatch_item_id) for i in overridden],
        },
    )
    return order


@transaction.atomic
def reconcile_dispatch_orders(client):
    """Bring a member's dispatch orders in line with their housing cases.

    Called ONCE per client after an import, from the same places as
    ``reconcile_internal_service_authorization`` -- so it sees the COMPLETE case
    picture rather than firing per row against a partial one. A member can arrive
    carrying several Home Remediation cases in one payload.

    Only ever CREATES: a DispatchItem for each housing work-order case, once the
    member has an assessment order. Cases that arrive first simply wait, and are
    picked up here on a later import or when the assessment is created. Assembling
    items into a WORK ORDER stays a deliberate agent action -- an import must never
    dispatch a vendor by itself.

    Never raises -- a dispatch hiccup must not fail a case import, the same
    contract the food reconcile already honours.
    """
    try:
        assessment = assessment_order_for(client)
        if assessment is None:
            return []
        return sync_dispatch_items(assessment)
    except Exception:  # noqa: BLE001 - never fail the import
        logger.exception("reconcile_dispatch_orders failed for client %s", client.pk)
        return []


# ── the service-area gate ────────────────────────────────────────────────────

def not_dispatchable_reason(order):
    """Why this order must NOT be sent to the vendor yet, or "" when it may be.

    Currently one rule, and it is the one that bit: an assessment whose dwelling
    ZIP is outside the service area was reaching a vendor's work list. A vendor
    cannot service an address we do not cover, and the trip is billable whether or
    not the visit was ever possible.

    IT IS NOT A CREATION ERROR. The order has to exist so an agent can CORRECT the
    address -- editing is allowed while an order is PENDING_SCHEDULE, and refusing
    to create it would mean re-entering the whole wizard to fix one field. So the
    order is created and simply withheld until the ZIP is in range.

    Returns a REASON rather than a bool so the CRM can say which address is wrong
    and the vendor API can log what it withheld.
    """
    from . import service_area

    area = service_area.order_service_area(order)

    # WITHHELD ONLY WHEN WE KNOW IT IS OUT OF AREA -- reason "out_of_area". The two
    # near misses are deliberately NOT withheld:
    #
    #   no_zip           we do not know where the dwelling is, which is not the same
    #                    as knowing we do not cover it. An order can carry a full
    #                    street address with the ZIP left blank and still be a
    #                    perfectly serviceable visit. Withholding these broke 14
    #                    existing vendor tests whose fixtures are ordinary orders
    #                    with no address -- the rest of the system treats those as
    #                    valid, and it is right.
    #   not configured   an empty ServiceZipCode table is inert everywhere else (see
    #                    is_zip_out_of_range), and a gate that withheld every order
    #                    on a fresh database would be this bug's twin. Handled
    #                    upstream: housing_area_check answers in_service_area=True.
    #
    # So this says "we cover somewhere, and it is not there".
    if (area.get("reason") or "") != "out_of_area":
        return ""
    zip_code = area.get("zip") or ""
    return (
        f"{zip_code} is outside our service area"
        if zip_code else "the dwelling address is outside our service area"
    )


def is_dispatchable(order):
    return not not_dispatchable_reason(order)


def recommended_products(assessment):
    """The product CATEGORIES this assessment recommended, from its questionnaire.

    An assessment recommends CATALOGUE PRODUCTS by option code (``window_ac``); a
    housing case is named after a product CATEGORY (``Home Remediation - Air
    Conditioner - Queens``). ``BillableItem.billing_category`` is the bridge, and it
    already lines up exactly:

        window_ac              -> "Air Conditioner"
        dehumidifier_portable  -> "De-humidifier"

    Returns ``{category: qty}``, or ``{}`` when nothing was recorded.

    ⚠ AN EMPTY RESULT MEANS "WE DO NOT KNOW", NOT "NOTHING WAS RECOMMENDED". Callers
    must not filter items down to nothing on the strength of it: an assessment
    predating the questionnaire, or one submitted on paper, has no interventions
    recorded, and hiding every item would leave an agent unable to raise a work order
    at all. Show everything in that case and say so.
    """
    from api.models import BillableItem

    form = getattr(assessment, "questionnaire", None)
    entries = (getattr(form, "interventions", None) or []) if form else []
    wanted = {}
    for entry in entries:
        code = (entry or {}).get("option")
        if not code:
            continue
        try:
            qty = int((entry or {}).get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        wanted[code] = wanted.get(code, 0) + qty
    if not wanted:
        return {}
    categories = {}
    rows = BillableItem.objects.filter(option_code__in=wanted).values_list(
        "option_code", "billing_category",
    )
    for code, category in rows:
        if not category:
            continue
        categories[category] = categories.get(category, 0) + wanted.get(code, 0)
    return categories


def annotate_items_for_picker(assessment, items):
    """Mark which items the work-order picker should offer, and pre-tick.

    Three facts per item, so the panel renders rather than decides:

    ``recommended``  this assessment asked for that product category
    ``preferred``    the ONE case chosen to fund it -- an OPEN case beats a closed
                     one, then a dispatchable beats an expired, then the newest
    ``recommended_qty``  how many the assessment asked for

    ⚠ WHY ONE CASE PER CATEGORY. MIRIAM ISRAEL holds THREE "Air Conditioner" cases
    (one open, two closed) against a recommended quantity of one. Offering all three
    asks the agent to know which of three identically-named rows is the live funding
    vehicle -- and she had 13 cases across five categories while the assessment
    recommended two products, so eleven of the rows were noise.

    A closed case is still eligible (``is_available`` deliberately ignores case
    status -- a Home Remediation case can close in Unite Us while the approved device
    still has to be fitted), so closed cases are ranked LOWER, never dropped.
    """
    wanted = recommended_products(assessment)
    # No recommendation recorded -> we do not know, so hide nothing.
    if not wanted:
        for item in items:
            item.recommended = True
            item.preferred = True
            item.recommended_qty = 0
        return {}

    by_category = {}
    for item in items:
        item.recommended = item.item in wanted
        item.recommended_qty = wanted.get(item.item, 0)
        item.preferred = False
        if item.recommended:
            by_category.setdefault(item.item, []).append(item)

    for category, group in by_category.items():
        group.sort(key=lambda i: (
            # An OPEN case first.
            0 if (i.case and (i.case.case_status or "") == "open") else 1,
            # Then one that needs no override.
            0 if i.is_available else 1,
            # Then the most recent, so a re-authorization beats a stale case.
            -(i.created_at.timestamp() if i.created_at else 0),
        ))
        # As many as were recommended -- a quantity of 2 needs two cases.
        for item in group[:max(1, wanted.get(category, 1))]:
            item.preferred = True
    return wanted


def _parse_window_rows(rows):
    """``[{date, start_time, end_time}]`` -> validated tuples. Raises ValueError.

    Times are parsed strictly rather than coerced: a window the vendor cannot honour
    is worse than one they were made to re-enter, and "9" could mean 09:00 or 21:00.
    """
    from datetime import date as date_cls, datetime, time as time_cls

    out = []
    for row in rows or []:
        raw_date = (row or {}).get("date")
        raw_start = (row or {}).get("start_time")
        raw_end = (row or {}).get("end_time")
        if not (raw_date and raw_start and raw_end):
            raise ValueError("each window needs a date, a start time and an end time")
        try:
            day = (
                raw_date if isinstance(raw_date, date_cls)
                else datetime.strptime(str(raw_date), "%Y-%m-%d").date()
            )
            start = (
                raw_start if isinstance(raw_start, time_cls)
                else datetime.strptime(str(raw_start)[:5], "%H:%M").time()
            )
            end = (
                raw_end if isinstance(raw_end, time_cls)
                else datetime.strptime(str(raw_end)[:5], "%H:%M").time()
            )
        except (TypeError, ValueError):
            raise ValueError(
                "a window needs date YYYY-MM-DD and times HH:MM"
            ) from None
        if end <= start:
            raise ValueError(f"{day}: the end time must be after the start time")
        out.append((day, start, end))
    return out


@transaction.atomic
def set_install_windows(assessment, rows):
    """Replace the vendor's tentative INSTALL windows on an assessment order.

    Optional, and offered while the vendor is still standing in the dwelling -- which
    is the only moment anyone knows what the job needs. There is no work order yet to
    attach them to, so they live on the assessment and are COPIED to each work order
    created from it (see ``copy_install_windows``).

    ⚠ THREE DISTINCT DATES when any are given, counted as DATES not rows -- three
    windows on one Tuesday is one option, not three, and the whole point is to give
    the scheduler a real choice. Zero is allowed: this is optional, and a vendor who
    cannot commit to anything should not be forced to invent dates.

    Replaces rather than appends, so re-submitting a corrected set does not leave the
    old one alongside it.
    """
    from api.models import DispatchAvailabilityWindow, WindowPurpose

    parsed = _parse_window_rows(rows)
    if parsed and len({day for day, _s, _e in parsed}) < 3:
        raise ValueError(
            "offer at least three DIFFERENT dates (several times on one day counts "
            "as one option)"
        )
    DispatchAvailabilityWindow.objects.filter(
        dispatch_order=assessment, purpose=WindowPurpose.INSTALL,
    ).delete()
    return [
        DispatchAvailabilityWindow.objects.create(
            dispatch_order=assessment, purpose=WindowPurpose.INSTALL,
            date=day, start_time=start, end_time=end,
        )
        for day, start, end in parsed
    ]


def install_windows(order):
    """The INSTALL windows on an order, in date order."""
    from api.models import WindowPurpose

    return list(
        order.availability_windows.filter(purpose=WindowPurpose.INSTALL)
        .order_by("date", "start_time")
    )


def copy_install_windows(assessment, work_order):
    """Copy the assessment's install windows onto a new work order.

    ⚠ COPIED, NOT SHARED. One assessment can produce several work orders -- the air
    conditioner now, the heater when its case is authorised -- and rescheduling the
    second must not move the first. Each order owns its own rows and can be edited
    independently.

    Silent when there are none: the windows are optional and a work order with no
    suggestions is simply scheduled from scratch.
    """
    from api.models import DispatchAvailabilityWindow, WindowPurpose

    rows = install_windows(assessment)
    if not rows:
        return []
    return DispatchAvailabilityWindow.objects.bulk_create([
        DispatchAvailabilityWindow(
            dispatch_order=work_order, purpose=WindowPurpose.INSTALL,
            date=r.date, start_time=r.start_time, end_time=r.end_time,
        )
        for r in rows
    ])

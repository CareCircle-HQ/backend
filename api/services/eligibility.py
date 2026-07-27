"""Import-time member eligibility evaluation + disposition.

Single source of truth for the CareCircle eligibility gates that run on the
client import (and, later, the daily Unite Us pull + extension client save).
Decisions are **client-based**: the caller must have persisted ALL of a client's
rows (insurances, social-care coverages, addresses) first, then call
``reconcile_client_eligibility`` ONCE per client -- never per import row.

Two layers:
  * PURE evaluation (:func:`evaluate_client`) -- reads the client's persisted
    relations and returns a :class:`Verdict`; no writes.
  * DISPOSITION (:func:`reconcile_client_eligibility`) -- applies the verdict:
    sets the ``INELIGIBLE`` lifecycle stage, writes a self-descriptive member
    note + a timeline event, and STOPS future deliveries so an ineligible member
    can never land in a new Purchase Order. Idempotent: it only acts on a
    transition, and recovers a member whose data later passes the checks.

Gates (all CareCircle-UNFIXABLE => hard off-ramp; the Unite Us case must be
closed by an agent -- the import only flags, it never calls Unite Us):
  * Medical insurance expired OR missing.
  * Wrong Medicaid TYPE (MLTC/MAP/FFS in the plan name; all-bad rule) --
    detection reused from :mod:`api.services.warnings`.
  * Primary/delivery address Out of Range (ZIP or State).

Expiry is DATE-BASED on ``expired_at`` and IGNORES the stored ``status``: the
export leaves ``record_status`` = "active" on policies whose end date is in the
past. A blank/None end date => active; year 9999 => never expires; a date before
today => expired.
"""

import hashlib
from dataclasses import dataclass, field

from django.utils import timezone

from api.models import AddressType, ClientStage, InsurancePlanType

# Coverage end-date sentinel: year 9999 means "never expires".
LIFETIME_SENTINEL_YEAR = 9999


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------
def _as_date(value):
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


def coverage_expired(end_dt, *, today=None):
    """Date-based expiry, ignoring any stored status.

    None / blank => not expired (active). Year 9999 => never expires. A date
    strictly before ``today`` => expired.
    """
    if end_dt is None:
        return False
    if getattr(end_dt, "year", None) == LIFETIME_SENTINEL_YEAR:
        return False
    end = _as_date(end_dt)
    return end is not None and end < (today or timezone.localdate())


def medical_insurance_reason(client, *, today=None):
    """Ineligibility reason for medical insurance, or "" when covered.

    Ineligible when the client has NO medical insurance on file, or when EVERY
    insurance policy is expired (date-based). A single non-expired policy clears
    the gate.
    """
    plans = list(client.insurances.all())
    if not plans:
        return "no medical insurance on file"
    if all(coverage_expired(p.expired_at, today=today) for p in plans):
        return "all medical insurance plans are expired"
    return ""


def medicaid_type_reason(client):
    """Ineligibility reason for a wrong Medicaid type (MLTC/MAP/FFS), or "".

    Reuses the all-bad detector from the warnings service so the warning and the
    eligibility gate can never drift.
    """
    from api.services.warnings import (
        _MEDICAID_INELIGIBLE_TERMS,
        member_wrong_medicaid_types,
    )

    bad = member_wrong_medicaid_types(client)
    if not bad:
        return ""
    terms = "/".join(_MEDICAID_INELIGIBLE_TERMS)
    return f"Medicaid plan type not served ({terms}): {', '.join(sorted(set(bad)))}"


def _range_addresses(client):
    """The client's PRIMARY (Current/Home) + DELIVERY addresses -- the ones the
    coverage gate judges."""
    wanted = {AddressType.CURRENT, AddressType.HOME, AddressType.DELIVERY}
    return [a for a in client.addresses.all() if a.type in wanted]


def address_range_reason(client, *, zips=None, states=None):
    """Ineligibility reason when a primary/delivery address is Out of Range
    (ZIP or State), or "". Checks the ZIP against the excluded-ZIP list and the
    state against the served-states allow-list."""
    from api.services.service_area import excluded_zips, is_zip_excluded
    from api.services.state_area import allowed_state_codes, is_state_allowed

    if zips is None:
        zips = excluded_zips()
    if states is None:
        states = allowed_state_codes()
    for a in _range_addresses(client):
        label = (a.type or "address").replace("_", " ")
        if a.zip and is_zip_excluded(a.zip, excluded=zips):
            return f"{label} ZIP {(a.zip or '').strip()[:5]} is outside the coverage area"
        if a.state and not is_state_allowed(a.state, allowed=states):
            return f"{label} state {a.state} is not served"
    return ""


def social_coverage_reason(client, *, today=None):
    """Reason a RECOVERABLE social-care-coverage hold is warranted, or "".

    Expired or missing social coverage is fixable by CareCircle, so it is a
    recoverable hold (pause), NOT a hard ineligibility.
    """
    covs = list(client.social_care_coverages.all())
    if not covs:
        return "no social care coverage on file"
    if all(coverage_expired(c.expired_at, today=today) for c in covs):
        return "all social care coverage is expired"
    return ""


@dataclass
class Verdict:
    """Outcome of the eligibility evaluation for one client."""

    ineligible: bool = False
    reasons: list = field(default_factory=list)          # hard, unfixable
    needs_hold: bool = False
    hold_reasons: list = field(default_factory=list)     # recoverable (social)


def evaluate_client(client, *, today=None, zips=None, states=None):
    """Evaluate every gate for a fully-persisted client. Pure (no writes)."""
    reasons = []
    for reason in (
        medical_insurance_reason(client, today=today),
        medicaid_type_reason(client),
        address_range_reason(client, zips=zips, states=states),
    ):
        if reason:
            reasons.append(reason)
    hold = social_coverage_reason(client, today=today)
    return Verdict(
        ineligible=bool(reasons),
        reasons=reasons,
        needs_hold=bool(hold),
        hold_reasons=[hold] if hold else [],
    )


# ---------------------------------------------------------------------------
# Disposition (writes)
# ---------------------------------------------------------------------------
def _write_client_system_note(client, body, *, author_name="System"):
    """Append a deduped SYSTEM note to THIS client (not the household primary).
    The content_hash guard makes re-imports with the same body idempotent."""
    from api.models import Note, NoteSource

    chash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if Note.objects.filter(
        client=client, source=NoteSource.SYSTEM, content_hash=chash
    ).exists():
        return
    Note.objects.create(
        client=client,
        source=NoteSource.SYSTEM,
        author_name=author_name or "System",
        body=body,
        content_hash=chash,
    )


def _set_client_stage(client, target, *, actor=None):
    """Set the client's lifecycle stage + log a StageEvent (used for the
    eligibility off-ramps, which are not produced by the funnel derivation)."""
    from api.models import StageEntityType, StageEvent, StageEventSource

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
        actor=actor,
    )


_INELIGIBLE_HOLD_NOTE = "Auto-hold: member marked Ineligible by import."
# Distinct hold note so the RECOVERABLE coverage hold can be reversed on its own
# (``_resume_auto_paused_enrollment`` matches on this prefix) without touching an
# ineligibility hold, a denial/closure hold, or a manual Place-on-Hold.
_COVERAGE_HOLD_NOTE = "Auto-hold: social care coverage expired/missing by import."
_COVERAGE_RESUME_NOTE = "Auto-resumed: social care coverage restored."


def _stop_future_deliveries(client, *, to_hold=True, note=_INELIGIBLE_HOLD_NOTE, actor=None):
    """Truncate every governing enrollment's future deliveries so the member
    can't land in a new PO, and (optionally) place the household On Hold -- the
    proven closure step, minus the cancel. ``note`` labels the hold StageEvent so
    the matching auto-resume can find it. Returns the paused enrollments."""
    from api.models import EnrollmentStage
    from api.services.lifecycle import (
        ENROLLMENT_TRANSITIONS,
        _governing_enrollments,
        advance_enrollment,
    )
    from api.services.orders import truncate_future_deliveries

    paused = []
    for enr in _governing_enrollments(client):
        try:
            truncate_future_deliveries(enr)
        except Exception:  # pragma: no cover - defensive
            pass
        if not to_hold:
            continue
        if EnrollmentStage.ON_HOLD in ENROLLMENT_TRANSITIONS.get(
            EnrollmentStage(enr.stage), set()
        ):
            try:
                advance_enrollment(
                    enr, EnrollmentStage.ON_HOLD, actor=actor, note=note,
                )
                paused.append(enr)
            except Exception:  # pragma: no cover - defensive
                pass
    return paused


def _coverage_held_enrollments(client):
    """Governing enrollments currently ON_HOLD *because of* the recoverable
    coverage hold (their most recent hold StageEvent carries the coverage note).
    Used to detect an active coverage hold for idempotency + recovery."""
    from api.models import EnrollmentStage, StageEvent
    from api.services.lifecycle import _governing_enrollments

    held = []
    for enr in _governing_enrollments(client):
        try:
            if EnrollmentStage(enr.stage) != EnrollmentStage.ON_HOLD:
                continue
        except ValueError:  # pragma: no cover - defensive
            continue
        last_hold = (
            StageEvent.objects.filter(
                enrollment=enr, to_stage=EnrollmentStage.ON_HOLD
            )
            .order_by("-entered_at")
            .first()
        )
        if last_hold and (last_hold.note or "").startswith(_COVERAGE_HOLD_NOTE):
            held.append(enr)
    return held


def _apply_coverage_hold(client, verdict, *, actor, author, today_str, source):
    """Place the member on the RECOVERABLE coverage hold: truncate future
    deliveries (always, so no PO leak) and, on the transition IN, pause the
    household + write a member note and timeline event. Idempotent: re-imports of
    an already-held member only re-truncate."""
    from api.services import timeline

    already = bool(_coverage_held_enrollments(client))
    paused = _stop_future_deliveries(
        client, to_hold=not already, note=_COVERAGE_HOLD_NOTE, actor=actor,
    )
    if already or not paused:
        return
    reasons = "; ".join(verdict.hold_reasons) or "social care coverage expired/missing"
    _write_client_system_note(
        client,
        (
            f"Service paused on {today_str}: {reasons}. Reversible \u2014 renewed "
            "social care coverage resumes service."
        ),
        author_name=author,
    )
    timeline.event_for_member_coverage_hold(
        client, reasons=verdict.hold_reasons, source=source, actor=author,
    )


def _clear_coverage_hold(client, *, actor, author, today_str, source):
    """Reverse a coverage hold once coverage recovers: resume each coverage-held
    enrollment to the stage it was held from, then write a recovery note +
    timeline event. No-op when no coverage hold is active."""
    from api.services import timeline
    from api.services.lifecycle import _resume_auto_paused_enrollment

    held = _coverage_held_enrollments(client)
    if not held:
        return
    for enr in held:
        _resume_auto_paused_enrollment(
            enr, actor=actor, hold_note=_COVERAGE_HOLD_NOTE,
            resume_note=_COVERAGE_RESUME_NOTE,
        )
    _write_client_system_note(
        client,
        f"Service resumed on {today_str}: social care coverage restored.",
        author_name=author,
    )
    timeline.event_for_member_coverage_restored(client, source=source, actor=author)


def reconcile_client_eligibility(client, *, actor=None, actor_label="", source=None, today=None):
    """Evaluate + apply the eligibility verdict for one fully-persisted client.

    Idempotent, and safe to call after every client upsert:
      * Newly ineligible -> set INELIGIBLE, write a member note + timeline event,
        and stop future deliveries (truncate + On Hold) so no new PO includes
        them.
      * Recovered (was INELIGIBLE, now passes) -> restore the derived stage +
        write a recovery note + timeline event.
    Returns the resulting :class:`~api.models.ClientStage` value (or None).
    """
    from api.history import ChangeSource
    from api.services import timeline
    from api.services.lifecycle import derive_client_stage

    if client is None:
        return None
    verdict = evaluate_client(client, today=today)
    src = source or ChangeSource.IMPORT
    author = actor_label or "System"
    today_str = (today or timezone.localdate()).isoformat()

    if verdict.ineligible:
        if client.lifecycle_stage != ClientStage.INELIGIBLE:
            _set_client_stage(client, ClientStage.INELIGIBLE, actor=actor)
            _write_client_system_note(
                client,
                (
                    f"Marked Ineligible on {today_str}: {'; '.join(verdict.reasons)}. "
                    "This can't be fixed in the CRM \u2014 the Unite Us case must be "
                    "closed by an agent."
                ),
                author_name=author,
            )
            timeline.event_for_member_ineligible(
                client, reasons=verdict.reasons, source=src, actor=author,
            )
        # Always stop future deliveries (idempotent) so an ineligible member is
        # excluded from every new PO, even across re-imports.
        _stop_future_deliveries(client)
        return ClientStage.INELIGIBLE

    # Not ineligible: recover a previously-ineligible member.
    if client.lifecycle_stage == ClientStage.INELIGIBLE:
        target = derive_client_stage(client, ignore_sticky=True)
        _set_client_stage(client, target, actor=actor)
        _write_client_system_note(
            client,
            f"Eligibility restored on {today_str}: the eligibility checks now pass.",
            author_name=author,
        )
        timeline.event_for_member_eligibility_restored(client, source=src, actor=author)

    # Recoverable social-care-coverage hold: pause (reversible) when coverage is
    # expired/missing, resume when it is restored. Runs only once the hard gates
    # pass, so an ineligible member is never double-handled.
    if verdict.needs_hold:
        _apply_coverage_hold(
            client, verdict, actor=actor, author=author,
            today_str=today_str, source=src,
        )
    else:
        _clear_coverage_hold(
            client, actor=actor, author=author, today_str=today_str, source=src,
        )

    return client.lifecycle_stage

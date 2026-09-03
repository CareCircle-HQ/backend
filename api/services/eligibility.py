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
    from api.services.service_area import service_zips, is_zip_out_of_range
    from api.services.state_area import allowed_state_codes, is_state_allowed

    if zips is None:
        zips = service_zips()
    if states is None:
        states = allowed_state_codes()
    for a in _range_addresses(client):
        label = (a.type or "address").replace("_", " ")
        if a.zip and is_zip_out_of_range(a.zip, service=zips):
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
    from api.services.lifecycle import stage_event_actor
    StageEvent.objects.create(
        entity_type=StageEntityType.CLIENT,
        client=client,
        from_stage=current or "",
        to_stage=target,
        source=StageEventSource.AUTO,
        actor=stage_event_actor(actor),
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

    # Machine trigger for the timeline history, derived from the hold note.
    if note.startswith(_COVERAGE_HOLD_NOTE):
        trigger = "eligibility.coverage_expired"
    elif note.startswith(_INELIGIBLE_HOLD_NOTE):
        trigger = "eligibility.ineligible"
    else:
        trigger = "eligibility.hold"

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
                    trigger=trigger,
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
    """Place the member on the RECOVERABLE coverage hold. Pauses the affected
    member(s) INDIVIDUALLY and drops them from the schedule (the whole household
    is held only when it's the member's sole member); on the transition IN it also
    records the household-level coverage timeline event. Idempotent."""
    from api.services import timeline

    already = any(p.eligibility_paused for p in _live_member_profiles(client))
    handled = _pause_members_for_eligibility(
        client, verdict.hold_reasons, kind="coverage",
        actor=actor, author=author, today_str=today_str,
    )
    if not handled:
        # No member profile to pause -> legacy whole-enrollment hold.
        paused = _stop_future_deliveries(
            client, to_hold=not already, note=_COVERAGE_HOLD_NOTE, actor=actor,
        )
        if not paused:
            return
    if already:
        return
    timeline.event_for_member_coverage_hold(
        client, reasons=verdict.hold_reasons, source=source, actor=author,
    )


def _clear_coverage_hold(client, *, actor, author, today_str, source):
    """Reverse a coverage hold once coverage recovers: un-pause the member(s) that
    were eligibility-paused, rebuild their schedule, and resume the program if the
    all-paused hold had fired. Also reverses any legacy whole-enrollment coverage
    hold. No-op when nothing is held."""
    from api.services import timeline
    from api.services.lifecycle import _resume_auto_paused_enrollment

    had_member_pause = any(p.eligibility_paused for p in _live_member_profiles(client))
    _unpause_members_for_eligibility(
        client, actor=actor, author=author, today_str=today_str,
    )
    # Legacy whole-enrollment coverage hold (no member profile path).
    for enr in _coverage_held_enrollments(client):
        _resume_auto_paused_enrollment(
            enr, actor=actor, hold_note=_COVERAGE_HOLD_NOTE,
            resume_note=_COVERAGE_RESUME_NOTE,
        )
        had_member_pause = True
    if had_member_pause:
        timeline.event_for_member_coverage_restored(client, source=source, actor=author)


def _live_member_profiles(client):
    """This client's dietary profiles on LIVE (non-terminal) enrollments."""
    from api.models import EnrollmentStage, MemberDietaryProfile

    terminal = [
        EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED, EnrollmentStage.DISREGARDED,
        # A parked reauthorization extension is not a live serving enrollment.
        EnrollmentStage.SCHEDULED_EXTENSION,
    ]
    return list(
        MemberDietaryProfile.objects.filter(client=client)
        .select_related("enrollment", "client")
        .exclude(enrollment__stage__in=terminal)
    )


def _member_display_name(profile):
    c = getattr(profile, "client", None)
    name = f"{getattr(c, 'first_name', '')} {getattr(c, 'last_name', '')}".strip()
    return name or (profile.member_name or "this member")


def _pause_members_for_eligibility(client, reasons, *, kind, actor, author, today_str):
    """Pause THIS client's household-member profile(s) for an eligibility problem
    -- instead of holding the WHOLE household. For each live profile: mark the
    member Paused (eligibility-driven), clear its kitchen result, drop them from
    the delivery calendar (so no future PO includes them), and write a
    self-descriptive SYSTEM note (WHY + WHAT changed/was discovered). The program
    is placed On Hold ONLY when EVERY member ends up paused -- i.e. the member IS
    the household -- via ``_reconcile_all_paused_hold``.

    Idempotent: the note/timeline fire only on the transition into the pause.
    Returns True when at least one profile was handled (False -> caller falls back
    to the legacy whole-enrollment hold when the client has no member profile).
    """
    from api.models import MemberStatus, Note, NoteSource
    from api.portal.views_members import _reconcile_all_paused_hold
    from api.services import timeline
    from api.services.orders import sync_delivery_calendar

    profiles = _live_member_profiles(client)
    if not profiles:
        return False
    reason_text = "; ".join(reasons) or (
        "ineligible" if kind == "ineligible" else "social care coverage expired/missing"
    )
    for mv in profiles:
        enr = mv.enrollment
        newly = not (mv.status == MemberStatus.PAUSED and mv.eligibility_paused)
        if newly:
            mv.status = MemberStatus.PAUSED
            mv.eligibility_paused = True
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            mv.save(update_fields=[
                "status", "eligibility_paused", "kitchen_meal_type",
                "kitchen_food_notes", "status_changed_at", "updated_at",
            ])
        # Paused members are excluded from the schedule; resync drops their future
        # (non-batched) occurrences so they leave the next Purchase Order.
        try:
            sync_delivery_calendar(enr)
        except Exception:  # pragma: no cover - defensive
            pass
        if newly:
            name = _member_display_name(mv)
            if kind == "ineligible":
                body = (
                    f"Paused {name} on {today_str} and removed them from the delivery "
                    f"schedule. Why: marked Ineligible by the import -- {reason_text}. "
                    "They are excluded from future Purchase Orders. This can't be fixed "
                    "in the CRM -- the Unite Us case must be closed by an agent; service "
                    "resumes automatically if the data later passes."
                )
            else:
                body = (
                    f"Paused {name} on {today_str} and removed them from the delivery "
                    f"schedule. Why: {reason_text}. They are excluded from future "
                    "Purchase Orders. Reversible -- renewed coverage resumes this member."
                )
            if mv.client_id:
                Note.objects.create(
                    client=mv.client, source=NoteSource.SYSTEM,
                    author_name=author, body=body,
                )
            try:
                timeline.event_for_member_paused(
                    mv, enrollment=enr, reason=reason_text, actor=author,
                )
            except Exception:  # pragma: no cover - defensive
                pass
        # Roll up to a PROGRAM hold only when every member is now paused.
        try:
            _reconcile_all_paused_hold(enr)
        except Exception:  # pragma: no cover - defensive
            pass
        # If some members remain servable, resume any STALE legacy whole-household
        # eligibility/coverage hold (from the old behavior, or a prior all-paused
        # state) so the rest of the household keeps serving. Note-scoped no-op
        # otherwise.
        profiles_all = list(enr.member_profiles.all())
        all_paused = bool(profiles_all) and all(
            p.status == MemberStatus.PAUSED for p in profiles_all
        )
        if not all_paused:
            from api.services.lifecycle import _resume_auto_paused_enrollment

            _resume_auto_paused_enrollment(
                enr, actor=actor, hold_note=_INELIGIBLE_HOLD_NOTE,
                resume_note=(
                    "Auto-resumed: only the ineligible member is paused; the rest "
                    "of the household continues service."
                ),
            )
            _resume_auto_paused_enrollment(
                enr, actor=actor, hold_note=_COVERAGE_HOLD_NOTE,
                resume_note=_COVERAGE_RESUME_NOTE,
            )
    return True


def _unpause_members_for_eligibility(client, *, actor, author, today_str):
    """Reverse an eligibility-driven member pause once the member passes the gates
    again: return the member(s) to Active, rebuild their delivery plan, write a
    recovery SYSTEM note (why + what changed), and resume the program if the
    all-paused hold had fired. No-op when the client has no eligibility-paused
    profiles."""
    from api.models import MemberStatus, Note, NoteSource
    from api.portal.views_members import _reconcile_all_paused_hold
    from api.services import timeline
    from api.services.orders import rebuild_delivery_calendar

    profiles = [p for p in _live_member_profiles(client) if p.eligibility_paused]
    if not profiles:
        return
    for mv in profiles:
        enr = mv.enrollment
        mv.status = MemberStatus.ACTIVE
        mv.eligibility_paused = False
        mv.save(update_fields=[
            "status", "eligibility_paused", "status_changed_at", "updated_at",
        ])
        # Resume the program first (if it was held because everyone was paused),
        # then rebuild this member's plan + calendar so they rejoin the next PO.
        try:
            _reconcile_all_paused_hold(enr)
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            rebuild_delivery_calendar(enr)
        except Exception:  # pragma: no cover - defensive
            pass
        name = _member_display_name(mv)
        body = (
            f"Reactivated {name} on {today_str} and rebuilt their delivery schedule. "
            "Why: the eligibility checks now pass, so they rejoin future Purchase Orders."
        )
        if mv.client_id:
            Note.objects.create(
                client=mv.client, source=NoteSource.SYSTEM,
                author_name=author, body=body,
            )
        try:
            timeline.event_for_member_unpaused(
                mv, enrollment=enr, reason="eligibility restored", actor=author,
            )
        except Exception:  # pragma: no cover - defensive
            pass


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
        # Persist the reasons on EVERY reconcile (idempotent) so the stored value
        # stays current and back-fills members flagged before this field existed
        # -- re-running the ext/CSV import populates it.
        if list(client.ineligible_reasons or []) != verdict.reasons:
            client.ineligible_reasons = verdict.reasons
            client.save(update_fields=["ineligible_reasons"])
        # Exclude the ineligible member from every future PO by pausing THEM
        # individually (idempotent) and dropping them from the schedule -- the
        # whole household is only held when this is its ONLY member. Falls back to
        # the legacy whole-enrollment stop when the client has no member profile.
        if not _pause_members_for_eligibility(
            client, verdict.reasons, kind="ineligible",
            actor=actor, author=author, today_str=today_str,
        ):
            # Enrich the whole-household hold note with WHO + WHY so the "Service
            # On Hold" timeline row explains itself (keeps the _INELIGIBLE_HOLD_NOTE
            # prefix so the auto-resume matcher still finds it).
            who = f"{client.first_name} {client.last_name}".strip()
            detail = "; ".join(verdict.reasons)
            hold_note = _INELIGIBLE_HOLD_NOTE
            if who or detail:
                hold_note = f"{_INELIGIBLE_HOLD_NOTE} {who}: {detail}".rstrip(": ").strip()
            _stop_future_deliveries(client, note=hold_note)
        return ClientStage.INELIGIBLE

    # Not ineligible: recover a previously-ineligible member.
    if client.lifecycle_stage == ClientStage.INELIGIBLE:
        target = derive_client_stage(client, ignore_sticky=True)
        _set_client_stage(client, target, actor=actor)
        # Clear the stored reasons now that the member passes the gates again.
        if client.ineligible_reasons:
            client.ineligible_reasons = []
            client.save(update_fields=["ineligible_reasons"])
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
        # Also reverse any hard-ineligibility member pause once the member fully
        # passes the gates (the client stage recovery above handles the CLIENT;
        # this returns the paused member(s) to service).
        _unpause_members_for_eligibility(
            client, actor=actor, author=author, today_str=today_str,
        )

    return client.lifecycle_stage


def apply_out_of_range_ineligibility(client, *, reason_detail, actor=None, actor_label="", today=None):
    """Mark ``client`` Not Eligible (INELIGIBLE) because a delivery / primary ZIP
    is outside the coverage area, and pause their member profile(s) exactly like
    the eligibility off-ramp (Paused + ``eligibility_paused``, kitchen result
    cleared, dropped from the delivery schedule).

    This is a HARD, sticky off-ramp: it is never auto-reversed when the ZIP later
    becomes serviceable -- an agent must resolve it (mirrors the import
    ineligibility off-ramp). ``reason_detail`` is a clear, ZIP-specific sentence
    used in the member note / timeline; the STORED ineligible reason is the stable
    ``SERVICE_AREA_REASON`` label so downstream views (e.g. the Care Management
    "Out of Range" tab) can detect a coverage-area ineligibility. Idempotent;
    returns True when the client was newly set INELIGIBLE."""
    from api.history import ChangeSource
    from api.services import timeline
    from api.services.service_area import SERVICE_AREA_REASON

    if client is None:
        return False
    author = actor_label or (getattr(actor, "name", "") if actor else "") or "System"
    today_str = (today or timezone.localdate()).isoformat()

    newly = client.lifecycle_stage != ClientStage.INELIGIBLE
    if newly:
        _set_client_stage(client, ClientStage.INELIGIBLE, actor=actor)
        _write_client_system_note(
            client,
            (
                f"Marked Not Eligible on {today_str}: {reason_detail} "
                "This can't be fixed in the CRM \u2014 an agent must review the case "
                "for closure."
            ),
            author_name=author,
        )
        try:
            timeline.event_for_member_ineligible(
                client, reasons=[SERVICE_AREA_REASON],
                source=ChangeSource.ADMIN, actor=author,
            )
        except Exception:  # pragma: no cover - never let history-logging break it
            pass
    # Merge the stable coverage-area reason in WITHOUT clobbering any other stored
    # ineligible reason (a member could be ineligible for several causes).
    reasons = list(client.ineligible_reasons or [])
    if SERVICE_AREA_REASON not in reasons:
        reasons.append(SERVICE_AREA_REASON)
        client.ineligible_reasons = reasons
        client.save(update_fields=["ineligible_reasons"])
    # Pause the member the eligibility way (idempotent; drops them from the
    # schedule + holds the program when every member is paused).
    _pause_members_for_eligibility(
        client, [reason_detail], kind="ineligible",
        actor=actor, author=author, today_str=today_str,
    )
    return newly

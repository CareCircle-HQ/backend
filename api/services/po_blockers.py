"""Detect members who SHOULD be in Purchase Orders but are blocked.

A member is expected to appear in POs when they have a live delivery PLAN
(:class:`~api.models.MemberDeliverySchedule` status SCHEDULED), an active member
profile, a non-excluded enrollment stage, and an assigned kitchen. That plan is
expanded into dated :class:`~api.models.OrderSchedule` occurrences, which PO
generation then filters by product kind. A member can silently drop out at any
of those steps.

:func:`classify_po_blockers` walks every active plan and classifies each member
by the reason they can't reach a live PO line, so the whole CLASS of "active
member missing from every PO" can be recognized at once. It is the shared
engine behind both the ``report_po_blockers`` management command and the
``/api/portal/po-blockers/`` endpoint.
"""
from django.db.models import Count, Prefetch
from django.utils import timezone

from api.models import (
    Case,
    CaseType,
    DeliveryCadence,
    MemberDeliverySchedule,
    OrderSchedule,
    ProductTypeKind,
    ScheduleStatus,
    ServiceAuthorizationStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from api.services.catalog import product_kind_for_enrollment, product_type_kind_for_name
from api.services.delivery import cadence_delivery_weekdays
from api.services.lifecycle import (
    governing_internal_case,
    open_internal_service_cases,
    pending_switch_case,
)
from api.services.orders import plan_built_kind

# Authorization statuses that permit us to (re)generate future deliveries.
_AUTHORIZED = {
    ServiceAuthorizationStatus.APPROVED,
    ServiceAuthorizationStatus.NOT_REQUIRED,
}

# Reason codes, ordered from "setup gap" to "fine". Everything except ``ok`` is
# a blocker that keeps the member out of Purchase Orders.
REASON_ORDER = [
    "no_kitchen",
    "lapsed_window_fixable",
    "needs_reauth",
    "program_switched",
    "no_future_generated",
    "cadence_weekday_mismatch",
    "kind_unresolved",
    "program_switch_pending",
    "duplicate_open_cases",
    "stale_case_link",
    "ok",
]
BLOCKED_REASONS = [r for r in REASON_ORDER if r != "ok"]

# Reasons a one-click fix can resolve server-side. The first group is healed by
# recompute_delivery_plan (weekdays + window + product, then calendar rebuild);
# stale_case_link is healed by repointing the enrollment's case.
_RECOMPUTE_REASONS = {
    "lapsed_window_fixable", "no_future_generated", "cadence_weekday_mismatch",
    "program_switched",
}
FIXABLE_REASONS = _RECOMPUTE_REASONS | {"stale_case_link"}

# Informational buckets: service is continuing correctly, but the state warrants
# visibility (an in-flight switch, or duplicate open cases to clean up in Unite
# Us). Not auto-fixable by us -- surfaced so logistics/agents can act.
INFO_REASONS = {"program_switch_pending", "duplicate_open_cases"}

REASON_LABELS = {
    "no_kitchen": "No kitchen assigned",
    "lapsed_window_fixable": "Lapsed window (fixable)",
    "needs_reauth": "Needs re-authorization",
    "program_switched": "Program switched (fixable)",
    "no_future_generated": "Calendar not generated",
    "cadence_weekday_mismatch": "Cadence/weekday mismatch",
    "kind_unresolved": "Product kind unresolved",
    "program_switch_pending": "Program switch pending",
    "duplicate_open_cases": "Duplicate open cases",
    "stale_case_link": "Stale case link",
    "ok": "OK",
}

REASON_DESCRIPTIONS = {
    "no_kitchen": "The household has no assigned kitchen, so no deliveries can be built. Assign a kitchen.",
    "lapsed_window_fixable": "Authorized with a future approval window, but the delivery plan window is unset or elapsed. Run backfill_delivery_calendar to regenerate.",
    "needs_reauth": "No approved authorization extending into the future. The case must be re-authorized (or the member off-boarded) before deliveries resume.",
    "no_future_generated": "The plan window covers the future yet no occurrences exist. A sync_delivery_calendars run should regenerate them.",
    "cadence_weekday_mismatch": "The delivery weekdays don't match the plan's cadence (e.g. a boxes→meals switch that left deliveries on Wednesday), so occurrences land on days no PO is cut for. Recompute realigns them.",
    "kind_unresolved": "Has future occurrences but the product kind (meals/boxes) can't be resolved, so PO generation drops them. Needs a data/case fix.",
    "program_switched": "The governing case's product kind (meals/boxes) now differs from the delivery plan — a switch was authorized (e.g. meals→boxes). The plan still delivers the old product. Apply the switch to rebuild the calendar for the new kind, window, and quantities.",
    "program_switch_pending": "Service is continuing on the current product, but a different-kind internal-service case is open and awaiting authorization (an in-flight switch). No action needed until Unite Us approves it; then apply the switch.",
    "duplicate_open_cases": "The household has more than one open internal-service case. Service is governed by the most favorable/newest one, but the superseded case should be closed in Unite Us for hygiene.",
    "stale_case_link": "Deliverable, but the enrollment's case doesn't point at the governing internal-service case (hygiene; not blocking after the preview fix).",
    "ok": "Has future occurrences with a resolvable product kind.",
}


def _classify_reason(*, kitchen_id, future, has_future_auth, plan_ends_on,
                     kind, plan_kind, governing_kind, plan_kind_authorized,
                     weekday_mismatch, switch_pending, open_case_count,
                     enrollment_case_id, governing_case_id, today):
    if kitchen_id is None:
        return "no_kitchen"
    if future == 0:
        if not has_future_auth:
            return "needs_reauth"
        if plan_ends_on is None or plan_ends_on < today:
            return "lapsed_window_fixable"
        return "no_future_generated"
    if kind is None:
        return "kind_unresolved"
    # An authorized meals<->boxes switch: the governing case's kind now differs
    # from what the plan was built as. Ready to apply (fixable) -- checked before
    # the weekday mismatch, which is just the visible symptom of the same flip.
    #
    # Guard against a FALSE switch: if an open, favorable case still matches the
    # plan's kind (``plan_kind_authorized``), the plan is still authorized and
    # the member simply has a parallel different-kind case -- that's a
    # ``duplicate_open_cases`` state, not a switch. Only when the plan-kind
    # program is truly retired (no open approved case of that kind) does the
    # governing different-kind case represent a real switch. This also keeps the
    # one-click fix from destructively flipping a correctly-served member.
    if (
        plan_kind is not None and governing_kind is not None
        and plan_kind != governing_kind
        and not plan_kind_authorized
    ):
        return "program_switched"
    if weekday_mismatch:
        return "cadence_weekday_mismatch"
    # Informational: service is continuing correctly on the current kind.
    if switch_pending:
        return "program_switch_pending"
    if open_case_count > 1:
        return "duplicate_open_cases"
    if governing_case_id and str(enrollment_case_id) != str(governing_case_id):
        return "stale_case_link"
    return "ok"


def _case_product_kind(case):
    """Best-effort Meals/Boxes kind for a SINGLE case (no enrollment context):
    the linked Program's ProductType, then a keyword on the program / service
    names. Returns a ProductTypeKind or None. Used to tell whether an open
    favorable case still backs the plan's kind (a real switch vs a parallel
    duplicate case)."""
    if case is None:
        return None
    program = case.program if getattr(case, "program_id", None) else None
    if program is not None and getattr(program, "product_type_id", None):
        pt = program.product_type
        if pt is not None:
            try:
                return ProductTypeKind(pt.type)
            except ValueError:
                pass
    for candidate in (
        program.name if program is not None else "",
        getattr(case, "program_name", "") or "",
        getattr(case, "service_type", "") or "",
    ):
        k = product_type_kind_for_name(candidate)
        if k:
            return k
    return None


def _weekday_mismatch(enr, cadence, kind):
    """True when the enrollment's delivery_weekdays don't match what the plan's
    cadence implies (e.g. a meals member on a mon_thu cadence whose weekdays are
    stuck on Wednesday from a prior boxes setup). The expected weekdays come from
    the Cadence settings table. A cadence with no fixed weekdays (once-a-week
    style) accepts any single weekday, so it never flags."""
    actual = set(enr.delivery_weekdays or [])
    if not actual or not cadence:
        return False
    expected = set(cadence_delivery_weekdays(cadence))
    if not expected:
        return False  # once-a-week style: any single weekday is valid
    return actual != expected


def classify_po_blockers(from_date=None, include_ok=False):
    """Return a list of per-member classification rows.

    Each row is a plain dict (see the keys assembled below). When
    ``include_ok`` is False (default) the ``ok`` rows are dropped so callers get
    only the blocked members.
    """
    today = from_date or timezone.localdate()

    # Future SCHEDULED occurrence counts keyed by member_profile id, in one
    # query (OrderSchedule.member is the MemberDietaryProfile).
    occ_counts = dict(
        OrderSchedule.objects.filter(
            status=ScheduleStatus.SCHEDULED,
            anticipated_delivery_date__gte=today,
        )
        .values_list("member_id")
        .annotate(n=Count("order_id"))
    )

    # Prefetch each client's cases so governing_internal_case() doesn't fire a
    # query per enrollment. select_related the program + its product type so the
    # per-case kind resolution (_case_product_kind) stays query-free.
    internal_cases = Prefetch(
        "enrollment__client__cases",
        queryset=Case.objects.select_related("program", "program__product_type"),
    )

    plans = (
        MemberDeliverySchedule.objects.filter(status=ScheduleStatus.SCHEDULED)
        .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        .exclude(member_profile__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .select_related(
            "enrollment", "enrollment__client", "enrollment__case",
            "member_profile", "member_profile__client",
        )
        .prefetch_related(internal_cases)
        .order_by("enrollment_id")
    )

    gov_cache = {}
    # enrollment pk -> (governing_kind, open_case_count, switch_pending,
    #                   favorable_open_kinds)
    enr_meta = {}
    rows = []
    # chunk_size is REQUIRED by Django when iterator() follows prefetch_related()
    # (raises ValueError otherwise on newer Django).
    for p in plans.iterator(chunk_size=1000):
        enr = p.enrollment
        m = p.member_profile
        if enr.pk not in gov_cache:
            gov_cache[enr.pk] = governing_internal_case(enr)
            governing_kind = product_kind_for_enrollment(enr)
            open_cases = open_internal_service_cases(enr.client)
            switch_pending = pending_switch_case(enr, governing_kind) is not None
            # Kinds still backed by an OPEN, favorable (approved/not-required)
            # case. If the plan's kind is in here, the plan is still authorized
            # and a different-kind governing case is a parallel duplicate, NOT a
            # switch.
            favorable_open_kinds = {
                _case_product_kind(c) for c in open_cases
                if c.service_authorization_status in _AUTHORIZED
            }
            favorable_open_kinds.discard(None)
            enr_meta[enr.pk] = (
                governing_kind, len(open_cases), switch_pending,
                favorable_open_kinds,
            )
        gov = gov_cache[enr.pk]
        governing_kind, open_case_count, switch_pending, favorable_open_kinds = enr_meta[enr.pk]
        auth_status = getattr(gov, "service_authorization_status", "") or ""
        auth_end = getattr(gov, "service_authorization_approval_ends_at", None)
        auth_end = auth_end.date() if auth_end else None
        future = occ_counts.get(p.member_profile_id, 0)
        kind = product_type_kind_for_name(enr.program_name) or governing_kind
        plan_kind = plan_built_kind(p)
        has_future_auth = (
            auth_status in _AUTHORIZED and auth_end is not None and auth_end >= today
        )
        gov_case_id = getattr(gov, "case_id", None) if gov else None
        mismatch = _weekday_mismatch(enr, p.delivery_days_cadence or "", kind)

        # The plan's own kind is still authorized when an open, favorable case
        # of that kind exists -> a different-kind governing case is a parallel
        # duplicate, not a switch.
        plan_kind_authorized = plan_kind is not None and plan_kind in favorable_open_kinds

        reason = _classify_reason(
            kitchen_id=enr.kitchen_id, future=future,
            has_future_auth=has_future_auth, plan_ends_on=p.ends_on,
            kind=kind, plan_kind=plan_kind, governing_kind=governing_kind,
            plan_kind_authorized=plan_kind_authorized,
            weekday_mismatch=mismatch, switch_pending=switch_pending,
            open_case_count=open_case_count, enrollment_case_id=enr.case_id,
            governing_case_id=gov_case_id, today=today,
        )
        if reason == "ok" and not include_ok:
            continue

        gov_program = getattr(getattr(gov, "program", None), "name", "") if gov else ""
        rows.append({
            "reason": reason,
            "reason_label": REASON_LABELS.get(reason, reason),
            "fixable": reason in FIXABLE_REASONS,
            "client_id": str(getattr(m, "client_id", "") or "") if m else "",
            "member_name": p.member_name or (m.member_name if m else ""),
            "enrollment_id": enr.pk,
            "stage": enr.stage,
            "kitchen_id": str(enr.kitchen_id) if enr.kitchen_id else "",
            "plan_ends_on": p.ends_on.isoformat() if p.ends_on else "",
            "future_occurrences": future,
            "program_name": enr.program_name or "",
            "governing_case_id": str(gov_case_id) if gov_case_id else "",
            "governing_program": gov_program or "",
            "auth_status": auth_status or "",
            "auth_window_end": auth_end.isoformat() if auth_end else "",
            "enrollment_case_id": str(enr.case_id) if enr.case_id else "",
            "kind": kind or "",
        })

    return rows


def summarize_po_blockers(rows):
    """Return {reason: count} for a list of classification rows."""
    counts = {}
    for r in rows:
        counts[r["reason"]] = counts.get(r["reason"], 0) + 1
    return counts


def remediate_enrollment_blocker(enr, reason, from_date=None):
    """Apply the server-side fix for a fixable blocker ``reason`` on ``enr``.

    * recompute reasons (lapsed window / not generated / weekday mismatch) ->
      :func:`recompute_delivery_plan` (weekdays + window + product, then rebuild).
    * ``stale_case_link`` -> repoint the enrollment to the governing case, then
      resync the calendar.

    Non-fixable reasons (no_kitchen / needs_reauth / kind_unresolved) return
    ``fixed=False`` with guidance. Returns a result dict.
    """
    from api.services.orders import recompute_delivery_plan, sync_delivery_calendar

    if reason in _RECOMPUTE_REASONS:
        res = recompute_delivery_plan(enr, from_date=from_date)
        return {"fixed": True, "action": "recompute_delivery_plan", "result": res}

    if reason == "stale_case_link":
        gov = governing_internal_case(enr)
        action = "sync"
        if gov is not None and str(enr.case_id) != str(gov.case_id):
            try:
                enr.case = gov
                enr.save(update_fields=["case"])
                action = "repoint_case"
            except Exception as exc:  # unique-per-case constraint, etc.
                return {"fixed": False, "message": f"Could not repoint case: {exc}"}
        res = sync_delivery_calendar(enr, from_date=from_date)
        return {"fixed": True, "action": action, "result": res}

    if reason in INFO_REASONS:
        return {"fixed": False, "message": REASON_DESCRIPTIONS.get(reason, "")}

    return {
        "fixed": False,
        "message": (
            f"'{reason}' isn't auto-fixable — it needs a manual action "
            f"(assign a kitchen, re-authorize the case, or fix the program)."
        ),
    }

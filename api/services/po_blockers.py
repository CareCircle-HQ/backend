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
    MemberDeliverySchedule,
    OrderSchedule,
    ScheduleStatus,
    ServiceAuthorizationStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from api.services.catalog import product_kind_for_enrollment, product_type_kind_for_name
from api.services.lifecycle import governing_internal_case

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
    "no_future_generated",
    "kind_unresolved",
    "stale_case_link",
    "ok",
]
BLOCKED_REASONS = [r for r in REASON_ORDER if r != "ok"]

REASON_LABELS = {
    "no_kitchen": "No kitchen assigned",
    "lapsed_window_fixable": "Lapsed window (fixable)",
    "needs_reauth": "Needs re-authorization",
    "no_future_generated": "Calendar not generated",
    "kind_unresolved": "Product kind unresolved",
    "stale_case_link": "Stale case link",
    "ok": "OK",
}

REASON_DESCRIPTIONS = {
    "no_kitchen": "The household has no assigned kitchen, so no deliveries can be built. Assign a kitchen.",
    "lapsed_window_fixable": "Authorized with a future approval window, but the delivery plan window is unset or elapsed. Run backfill_delivery_calendar to regenerate.",
    "needs_reauth": "No approved authorization extending into the future. The case must be re-authorized (or the member off-boarded) before deliveries resume.",
    "no_future_generated": "The plan window covers the future yet no occurrences exist. A sync_delivery_calendars run should regenerate them.",
    "kind_unresolved": "Has future occurrences but the product kind (meals/boxes) can't be resolved, so PO generation drops them. Needs a data/case fix.",
    "stale_case_link": "Deliverable, but the enrollment's case doesn't point at the governing internal-service case (hygiene; not blocking after the preview fix).",
    "ok": "Has future occurrences with a resolvable product kind.",
}


def _classify_reason(*, kitchen_id, future, has_future_auth, plan_ends_on,
                     kind, enrollment_case_id, governing_case_id, today):
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
    if governing_case_id and str(enrollment_case_id) != str(governing_case_id):
        return "stale_case_link"
    return "ok"


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
    # query per enrollment.
    internal_cases = Prefetch(
        "enrollment__client__cases",
        queryset=Case.objects.all(),
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
    rows = []
    # chunk_size is REQUIRED by Django when iterator() follows prefetch_related()
    # (raises ValueError otherwise on newer Django).
    for p in plans.iterator(chunk_size=1000):
        enr = p.enrollment
        m = p.member_profile
        if enr.pk not in gov_cache:
            gov_cache[enr.pk] = governing_internal_case(enr)
        gov = gov_cache[enr.pk]
        auth_status = getattr(gov, "service_authorization_status", "") or ""
        auth_end = getattr(gov, "service_authorization_approval_ends_at", None)
        auth_end = auth_end.date() if auth_end else None
        future = occ_counts.get(p.member_profile_id, 0)
        kind = product_type_kind_for_name(enr.program_name) or product_kind_for_enrollment(enr)
        has_future_auth = (
            auth_status in _AUTHORIZED and auth_end is not None and auth_end >= today
        )
        gov_case_id = getattr(gov, "case_id", None) if gov else None

        reason = _classify_reason(
            kitchen_id=enr.kitchen_id, future=future,
            has_future_auth=has_future_auth, plan_ends_on=p.ends_on,
            kind=kind, enrollment_case_id=enr.case_id,
            governing_case_id=gov_case_id, today=today,
        )
        if reason == "ok" and not include_ok:
            continue

        gov_program = getattr(getattr(gov, "program", None), "name", "") if gov else ""
        rows.append({
            "reason": reason,
            "reason_label": REASON_LABELS.get(reason, reason),
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

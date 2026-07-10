"""Delivery-order generation for an authorized enrollment.

When an :class:`~api.models.EnrollmentVerification` enters the ``AUTHORIZED``
(Accepted) stage, we expand the full delivery schedule across the case's
authorization window: one :class:`~api.models.OrderSchedule` per non-denied
member, per delivery date. Delivery dates are every date in the window whose
weekday is one the customer chose (``enrollment.delivery_weekdays``); the count
of chosen weekdays IS the deliveries-per-week.

Generation is idempotent: if the enrollment already has orders, it is a no-op,
so the verification-completion path and the nightly import path can both call
it safely (and repeatedly) without creating duplicates.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from api.models import (
    DeliveryOrder,
    EnrollmentStage,
    MemberStatus,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
    OrderSchedule,
    OrderStatus,
    ScheduleStatus,
    generate_household_group_code,
)

# Weekday code (stored in EnrollmentVerification.delivery_weekdays) -> the int
# returned by date.weekday() (Monday == 0 ... Sunday == 6).
_WEEKDAY_CODES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def _format_address(address):
    """Render an Address FK to a single-line text snapshot. Empty if no address."""
    if address is None:
        return ""
    parts = [address.street, address.city, address.state, address.zip]
    line = ", ".join(p for p in parts[:2] if p)
    tail = " ".join(p for p in parts[2:] if p)
    return ", ".join(p for p in (line, tail) if p)


def _delivery_window(enrollment):
    """(start_date, end_date) from the linked case's authorization approval
    window, or (None, None) if the case / dates are missing."""
    case = enrollment.case
    if case is None:
        return None, None
    start = case.service_authorization_approval_starts_at
    end = case.service_authorization_approval_ends_at
    if not start or not end:
        return None, None
    return start.date(), end.date()


def coverage_days(weekday, delivery_weekday_ints):
    """Number of days a delivery on ``weekday`` covers: the cyclic gap to the
    next delivery weekday in the cadence. The gaps over a week always sum to 7,
    so a Mon/Thu cadence yields 3 (Mon->Thu) then 4 (Thu->Mon). A single weekly
    delivery covers the full 7 days."""
    days = sorted(set(delivery_weekday_ints))
    if not days or weekday not in days:
        return 7
    i = days.index(weekday)
    nxt = days[(i + 1) % len(days)]
    return (nxt - weekday) % 7 or 7


def meals_for_delivery(weekday, delivery_weekday_ints, meals_per_day):
    """Per-delivery meal count = ``meals_per_day`` x the days that delivery
    covers (see :func:`coverage_days`)."""
    return (meals_per_day or 0) * coverage_days(weekday, delivery_weekday_ints)


def _weekday_ints(weekday_codes):
    """Convert a list of weekday codes ("mon", "thu", ...) to date.weekday()
    ints, ignoring anything unrecognized."""
    return [_WEEKDAY_CODES[w] for w in (weekday_codes or []) if w in _WEEKDAY_CODES]


def _delivery_dates(start, end, weekdays):
    """Every date in [start, end] whose weekday is one of ``weekdays`` (a list
    of weekday codes). Returns a sorted list of dates."""
    wanted = {_WEEKDAY_CODES[w] for w in weekdays if w in _WEEKDAY_CODES}
    if not wanted or start is None or end is None or end < start:
        return []
    dates = []
    d = start
    while d <= end:
        if d.weekday() in wanted:
            dates.append(d)
        d += timedelta(days=1)
    return dates


@transaction.atomic
def generate_orders_for_enrollment(enrollment):
    """Create the full delivery schedule for an authorized enrollment.

    Idempotent: returns ``[]`` immediately if orders already exist. Returns the
    list of created :class:`OrderSchedule` rows otherwise.
    """
    if enrollment.orders.exists():
        return []

    start, end = _delivery_window(enrollment)
    weekdays = enrollment.delivery_weekdays or []
    dates = _delivery_dates(start, end, weekdays)
    if not dates:
        return []

    # Out-of-orbit and paused members are excluded from all delivery orders.
    members = list(
        enrollment.member_profiles.select_related("client")
        .exclude(status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .all()
    )
    if not members:
        return []

    # One shared batch code reused across every delivery for this household, so
    # the kitchen / delivery company can group the household's food together.
    group_code = generate_household_group_code()
    address_text = _format_address(enrollment.delivery_address)

    orders = []
    for d in dates:
        for m in members:
            client = m.client
            orders.append(
                OrderSchedule(
                    enrollment=enrollment,
                    program_name=enrollment.program_name,
                    member=m,
                    member_name=m.member_name,
                    anticipated_delivery_date=d,
                    household=enrollment.household,
                    household_group_code=group_code,
                    status=OrderStatus.SCHEDULED,
                    delivery_address=address_text,
                    allergies=list(m.food_allergies or []),
                    restrictions=list(m.dietary_restrictions or []),
                    menu_type=m.menu_type,
                    kitchen_meal_type=m.kitchen_meal_type,
                    kitchen_food_notes=m.kitchen_food_notes,
                    member_phone=getattr(client, "client_phone_number", "") or "",
                    member_email=getattr(client, "client_email_address", "") or "",
                    how_many_meals_or_boxes=m.meals_per_delivery,
                )
            )
    return OrderSchedule.objects.bulk_create(orders)


@transaction.atomic
def generate_delivery_calendar(enrollment):
    """Expand each ``MemberDeliverySchedule`` (the recurring plan) into dated
    :class:`OrderSchedule` occurrences — the delivery calendar that PO
    generation later aggregates.

    Unlike :func:`generate_orders_for_enrollment`, this is driven by the
    per-member PLAN, so it honors each plan's ``starts_on`` (the first delivery
    date, which already encodes the cadence's first-delivery / Wednesday-skip
    rule) and ``ends_on``. Delivery weekdays come from the enrollment (auto-set
    from the matched CadenceRule).

    Idempotent: returns ``[]`` if the enrollment already has orders.
    """
    if enrollment.orders.exists():
        return []

    schedules = list(
        enrollment.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED)
        .select_related("member_profile", "member_profile__client", "kitchen")
    )
    if not schedules:
        return []

    weekdays = enrollment.delivery_weekdays or []
    weekday_ints = _weekday_ints(weekdays)
    group_code = generate_household_group_code()
    address_text = _format_address(enrollment.delivery_address)

    orders = []
    for sched in schedules:
        dates = _delivery_dates(sched.starts_on, sched.ends_on, weekdays)
        if not dates:
            continue
        m = sched.member_profile
        client = getattr(m, "client", None)
        for d in dates:
            # Meals: quantity is the daily rate x days this delivery covers
            # (e.g. 3/day -> 9 then 12 on a Mon/Thu cadence). Boxes: flat
            # per-delivery count.
            if sched.meals_per_day:
                qty = meals_for_delivery(d.weekday(), weekday_ints, sched.meals_per_day)
            else:
                qty = sched.prod_per_delivery
            orders.append(
                OrderSchedule(
                    enrollment=enrollment,
                    program_name=enrollment.program_name,
                    member=m,
                    member_name=sched.member_name or (m.member_name if m else ""),
                    anticipated_delivery_date=d,
                    household=enrollment.household,
                    household_group_code=group_code,
                    kitchen=sched.kitchen,
                    status=OrderStatus.SCHEDULED,
                    delivery_address=address_text,
                    allergies=list(getattr(m, "food_allergies", []) or []),
                    restrictions=list(getattr(m, "dietary_restrictions", []) or []),
                    menu_type=sched.menu_type or (m.menu_type if m else ""),
                    kitchen_meal_type=sched.kitchen_meal_type or (m.kitchen_meal_type if m else ""),
                    kitchen_food_notes=sched.kitchen_food_notes or (m.kitchen_food_notes if m else ""),
                    member_phone=getattr(client, "client_phone_number", "") or "",
                    member_email=getattr(client, "client_email_address", "") or "",
                    how_many_meals_or_boxes=qty,
                )
            )
    return OrderSchedule.objects.bulk_create(orders)


@transaction.atomic
def resync_scheduled_orders(*, enrollment=None, delivery_date=None, from_date=None):
    """Refresh future SCHEDULED OrderSchedule snapshots from the CURRENT member
    dietary profiles + the household's assigned kitchen. Returns the count of
    rows updated.

    OrderSchedule rows are point-in-time snapshots (kitchen, menu_type,
    allergies, kitchen_meal_type, ...). When a member's menu type / allergies or
    the household kitchen change AFTER the delivery calendar was built, the
    still-SCHEDULED occurrences keep the stale values -- so PO generation groups
    a reassigned member under the OLD kitchen and can wrongly flag a supported
    menu as "unsupported". This re-pulls the live values onto those rows. Only
    SCHEDULED rows are touched; dispatched/historical orders keep their snapshot.

    Scope with ``enrollment`` (one household) and/or ``delivery_date`` (a single
    date, e.g. the PO popup "refresh"). When no ``delivery_date`` is given,
    ``from_date`` (default today) limits it to future occurrences so past orders
    are never rewritten.
    """
    qs = OrderSchedule.objects.filter(status=OrderStatus.SCHEDULED)
    if enrollment is not None:
        qs = qs.filter(enrollment=enrollment)
    if delivery_date is not None:
        qs = qs.filter(anticipated_delivery_date=delivery_date)
    else:
        qs = qs.filter(
            anticipated_delivery_date__gte=from_date or timezone.localdate()
        )
    qs = qs.select_related("member", "enrollment")

    updated = 0
    for o in qs:
        m = o.member
        if m is None:
            continue
        new_kitchen_id = o.enrollment.kitchen_id if o.enrollment else o.kitchen_id
        fields = {
            "kitchen_id": new_kitchen_id,
            "menu_type": m.menu_type or "",
            "kitchen_meal_type": m.kitchen_meal_type or "",
            "kitchen_food_notes": m.kitchen_food_notes or "",
            "allergies": list(m.food_allergies or []),
            "restrictions": list(m.dietary_restrictions or []),
        }
        if all(getattr(o, f) == v for f, v in fields.items()):
            continue  # already in sync
        for f, v in fields.items():
            setattr(o, f, v)
        o.save(update_fields=[
            "kitchen", "menu_type", "kitchen_meal_type", "kitchen_food_notes",
            "allergies", "restrictions", "updated_at",
        ])
        updated += 1
    return updated


@transaction.atomic
def sync_delivery_calendar(enrollment, from_date=None):
    """Reconcile an enrollment's FUTURE delivery occurrences with its CURRENT
    member plans + dietary profiles. Idempotent; safe to call after any change.

    Unlike :func:`generate_delivery_calendar` (a one-shot no-op once orders
    exist), this keeps the dated calendar in step with later edits:

    * **adds** occurrences for any planned member/date missing from the calendar
      -- so a member added to the household, or new dates from a cadence change,
      are never left out of Purchase Orders;
    * **removes** future SCHEDULED occurrences no longer in the plan (e.g. dates
      dropped by a cadence change);
    * **refreshes** the kitchen / menu / allergy / quantity snapshots on the
      rows it keeps (see :func:`resync_scheduled_orders`).

    A (member, date) already batched into a PO -- i.e. a ``DeliveryOrder`` exists
    for that member's client on that date -- is NEVER added, removed, or altered,
    so committed orders stay stable. Past occurrences (before ``from_date``,
    default today) are left untouched.

    Returns ``{"added": n, "removed": n, "updated": n}``.
    """
    from_date = from_date or timezone.localdate()
    weekdays = enrollment.delivery_weekdays or []
    weekday_ints = _weekday_ints(weekdays)

    plans = list(
        enrollment.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED)
        .select_related("member_profile", "member_profile__client")
    )
    # Skip out-of-orbit / paused members: they carry a plan but are excluded
    # from POs.
    plans = [
        p for p in plans
        if p.member_profile
        and p.member_profile.status not in SERVICE_EXCLUDED_MEMBER_STATUSES
    ]

    existing = list(
        enrollment.orders.filter(
            status=OrderStatus.SCHEDULED, anticipated_delivery_date__gte=from_date,
        ).select_related("member")
    )
    existing_by_key = {
        (o.member_id, o.anticipated_delivery_date): o for o in existing
    }

    # (client_id, date) pairs already committed to a DeliveryOrder -- untouchable.
    client_by_profile = {
        p.member_profile_id: p.member_profile.client_id for p in plans
    }
    all_client_ids = [c for c in client_by_profile.values() if c]
    batched = set(
        DeliveryOrder.objects.filter(
            member_id__in=all_client_ids, expected_delivery_date__gte=from_date,
        ).values_list("member_id", "expected_delivery_date")
    )

    group_code = (
        existing[0].household_group_code if existing else generate_household_group_code()
    )
    address_text = _format_address(enrollment.delivery_address)

    def _qty(plan, d):
        if plan.meals_per_day:
            return meals_for_delivery(d.weekday(), weekday_ints, plan.meals_per_day)
        return plan.prod_per_delivery

    expected_keys = set()
    to_create = []
    updated = 0
    for plan in plans:
        m = plan.member_profile
        client = getattr(m, "client", None)
        client_id = client_by_profile.get(plan.member_profile_id)
        for d in _delivery_dates(plan.starts_on, plan.ends_on, weekdays):
            if d < from_date:
                continue
            key = (m.pk, d)
            expected_keys.add(key)
            if (client_id, d) in batched:
                continue  # committed to a PO -- leave it alone
            row = existing_by_key.get(key)
            if row is None:
                to_create.append(OrderSchedule(
                    enrollment=enrollment,
                    program_name=enrollment.program_name,
                    member=m,
                    member_name=plan.member_name or m.member_name,
                    anticipated_delivery_date=d,
                    household=enrollment.household,
                    household_group_code=group_code,
                    kitchen_id=enrollment.kitchen_id,
                    status=OrderStatus.SCHEDULED,
                    delivery_address=address_text,
                    allergies=list(m.food_allergies or []),
                    restrictions=list(m.dietary_restrictions or []),
                    menu_type=m.menu_type or "",
                    kitchen_meal_type=m.kitchen_meal_type or "",
                    kitchen_food_notes=m.kitchen_food_notes or "",
                    member_phone=getattr(client, "client_phone_number", "") or "",
                    member_email=getattr(client, "client_email_address", "") or "",
                    how_many_meals_or_boxes=_qty(plan, d),
                ))
            else:
                fields = {
                    "kitchen_id": enrollment.kitchen_id,
                    "menu_type": m.menu_type or "",
                    "kitchen_meal_type": m.kitchen_meal_type or "",
                    "kitchen_food_notes": m.kitchen_food_notes or "",
                    "allergies": list(m.food_allergies or []),
                    "restrictions": list(m.dietary_restrictions or []),
                    "how_many_meals_or_boxes": _qty(plan, d),
                }
                if any(getattr(row, f) != v for f, v in fields.items()):
                    for f, v in fields.items():
                        setattr(row, f, v)
                    row.save(update_fields=[
                        "kitchen", "menu_type", "kitchen_meal_type",
                        "kitchen_food_notes", "allergies", "restrictions",
                        "how_many_meals_or_boxes", "updated_at",
                    ])
                    updated += 1

    # Remove future occurrences no longer planned (and not already batched).
    stale_ids = [
        o.pk for key, o in existing_by_key.items()
        if key not in expected_keys
        and (client_by_profile.get(o.member_id), o.anticipated_delivery_date) not in batched
    ]
    removed = 0
    if stale_ids:
        removed = OrderSchedule.objects.filter(pk__in=stale_ids).delete()[0]

    if to_create:
        OrderSchedule.objects.bulk_create(to_create)

    return {"added": len(to_create), "removed": removed, "updated": updated}


def sync_active_calendars(from_date=None):
    """Reconcile the delivery calendar for every enrollment that currently has
    future occurrences (see :func:`sync_delivery_calendar`).

    Backs the PO popup "Refresh" and the ``sync_delivery_calendars`` command so
    no eligible member is ever missing from a Purchase Order: any member added
    to an already-active household, and any cadence/kitchen/dietary drift, is
    picked up. Brand-new households get their calendar at kitchen-assignment
    time, so they are already covered. Returns aggregate counts.
    """
    from api.models import EnrollmentVerification

    from_date = from_date or timezone.localdate()
    enr_ids = list(
        OrderSchedule.objects.filter(
            status=OrderStatus.SCHEDULED, anticipated_delivery_date__gte=from_date,
        ).values_list("enrollment_id", flat=True).distinct()
    )
    totals = {"enrollments": 0, "added": 0, "removed": 0, "updated": 0}
    for enr in EnrollmentVerification.objects.filter(pk__in=enr_ids).iterator():
        res = sync_delivery_calendar(enr, from_date=from_date)
        totals["enrollments"] += 1
        totals["added"] += res["added"]
        totals["removed"] += res["removed"]
        totals["updated"] += res["updated"]
    return totals


@transaction.atomic
def recompute_delivery_plan(enrollment, from_date=None):
    """Re-derive a household's delivery plan from its current inputs, then rebuild
    the dated calendar. THE single fix for delivery-date drift.

    Recomputes, in order:
      1. product **kind** (meals/boxes) via the robust resolver;
      2. **delivery weekdays** from that kind + the plan's cadence (boxes -> fixed
         Wednesday; meals -> the cadence's weekdays) -- healing a weekday/kind
         mismatch (e.g. a boxes->meals switch that left weekdays on Wednesday);
      3. the plan **window** (starts_on/ends_on) from the GOVERNING authorization
         -- healing an unset/elapsed window;
      4. product_type + per-delivery quantities.

    Then :func:`sync_delivery_calendar` rebuilds the future occurrences (dropping
    dates no longer planned, adding the new ones), never touching a date already
    batched into a PO. Returns the sync result dict ``{added, removed, updated}``.

    No-op-safe: if the household has no plan yet, it just reconciles whatever
    exists.
    """
    from api.services.catalog import product_kind_for_enrollment
    from api.services.delivery import current_household_cadence, update_household_cadence
    from api.services.lifecycle import governing_internal_case
    from api.models import DeliveryCadence

    cadence = current_household_cadence(enrollment)
    if not cadence:
        return sync_delivery_calendar(enrollment, from_date=from_date)

    case = governing_internal_case(enrollment) or enrollment.case
    kind = product_kind_for_enrollment(enrollment)
    # Preserve the agent-chosen weekday for a weekly cadence (any single weekday
    # is valid for once_a_week), so recompute never fights an intentional choice.
    once_weekday = None
    if cadence == DeliveryCadence.ONCE_A_WEEK:
        wd = [w for w in (enrollment.delivery_weekdays or []) if w in _WEEKDAY_CODES]
        once_weekday = wd[0] if wd else None

    update_household_cadence(
        enrollment, cadence=cadence, once_a_week_weekday=once_weekday,
        case=case, product_kind=kind,
    )
    return sync_delivery_calendar(enrollment, from_date=from_date)


def plan_built_kind(plan):
    """The product kind a delivery plan was BUILT as, read from its own snapshot
    (``meals_per_day`` for meals, ``prod_per_delivery`` for boxes) -- independent
    of the governing case, so it can be compared against the case's current kind
    to detect a meals<->boxes switch. Returns a ProductTypeKind or None."""
    from api.models import ProductTypeKind

    if plan is None:
        return None
    if plan.meals_per_day:
        return ProductTypeKind.MEALS
    if plan.prod_per_delivery:
        return ProductTypeKind.BOXES
    return None


@transaction.atomic
def heal_delivery_window(enrollment, from_date=None):
    """Auto-heal a SAME-KIND delivery-window drift.

    When the governing approved authorization window differs from the plan window
    (e.g. a re-approval extended the end date) AND the product kind is unchanged,
    recompute the plan + rebuild the calendar so the new dates flow into POs with
    no manual step. Returns the sync result, or ``None`` when there's nothing to
    do.

    NEVER switches product kind: a meals<->boxes change is human-confirmed via
    the PO Blockers 'program_switched' fix, not applied silently here.
    """
    from api.services.catalog import product_kind_for_enrollment
    from api.services.lifecycle import governing_internal_case

    plan = enrollment.delivery_schedules.first()
    if plan is None:
        return None
    gov = governing_internal_case(enrollment)
    end = getattr(gov, "service_authorization_approval_ends_at", None) if gov else None
    gov_end = end.date() if end else None
    if gov_end is None:
        return None

    gov_kind = product_kind_for_enrollment(enrollment)
    plan_kind = plan_built_kind(plan)
    if gov_kind is not None and plan_kind is not None and gov_kind != plan_kind:
        return None  # kind switch -> human confirm, never auto-flip
    if plan.ends_on == gov_end:
        return None  # window already in sync -- nothing to heal
    return recompute_delivery_plan(enrollment, from_date=from_date)


@transaction.atomic
def truncate_future_deliveries(enrollment, on_or_after=None):
    """Stop future deliveries by shortening every plan window to just before
    ``on_or_after`` (default today), then rebuilding the calendar.

    Used when a case closes with no authorization covering the future (and no
    in-flight switch), to prevent over-delivery past the authorization. Shortening
    the plan window -- not just deleting occurrences -- is required so the nightly
    ``sync_active_calendars`` doesn't regenerate them. PO-batched occurrences are
    preserved by :func:`sync_delivery_calendar`. Returns the sync result.
    """
    cutoff = on_or_after or timezone.localdate()
    prev = cutoff - timedelta(days=1)
    for p in enrollment.delivery_schedules.all():
        if p.ends_on is None or p.ends_on >= cutoff:
            p.ends_on = prev
            p.save(update_fields=["ends_on"])
    return sync_delivery_calendar(enrollment, from_date=cutoff)


@transaction.atomic
def close_duplicate_enrollment(enrollment, from_date=None):
    """Close a spurious DUPLICATE enrollment (a second enrollment for a client
    who already has the real, case-linked one).

    Cancels its delivery plans so the nightly sync won't regenerate anything,
    removes its FUTURE non-batched occurrences (occurrences already committed to
    a DeliveryOrder are left intact by :func:`sync_delivery_calendar`), and sets
    the enrollment to CANCELLED so it drops out of PO/delivery generation.

    The caller is responsible for confirming this enrollment is the duplicate to
    close (e.g. it is NOT the one on the governing internal-service case).
    Returns a summary dict.
    """
    cutoff = from_date or timezone.localdate()
    plans_cancelled = (
        enrollment.delivery_schedules.exclude(status=ScheduleStatus.CANCELLED)
        .update(status=ScheduleStatus.CANCELLED)
    )
    # With no SCHEDULED plans left, this drops every future non-batched
    # occurrence for the enrollment.
    calendar = sync_delivery_calendar(enrollment, from_date=cutoff)
    previous_stage = enrollment.stage
    if enrollment.stage != EnrollmentStage.CANCELLED:
        enrollment.stage = EnrollmentStage.CANCELLED
        enrollment.save(update_fields=["stage"])
    return {
        "enrollment_id": enrollment.pk,
        "previous_stage": previous_stage,
        "plans_cancelled": plans_cancelled,
        "calendar": calendar,
    }

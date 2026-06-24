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

from api.models import (
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

    members = list(
        enrollment.member_profiles.select_related("client").all()
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
                    member_phone=getattr(client, "client_phone_number", "") or "",
                    member_email=getattr(client, "client_email_address", "") or "",
                    how_many_meals_or_boxes=sched.prod_per_delivery,
                )
            )
    return OrderSchedule.objects.bulk_create(orders)

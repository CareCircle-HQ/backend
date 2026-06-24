"""Per-member delivery-plan setup for an accepted enrollment.

When a verification is completed and its case authorization is Accepted, we
create one :class:`~api.models.MemberDeliverySchedule` per household member.
The schedule is the durable recurring PLAN ("what this member gets each week")
that is later expanded into dated :class:`~api.models.OrderSchedule` occurrences
(the delivery calendar).

Cadence is assigned dynamically from the weekday the case becomes active (its
authorization approval start date) via the admin-editable
:class:`~api.models.CadenceRule` table, keyed by (product kind, accepted
weekday). The matched rule supplies the cadence, the delivery weekdays, the PO
weekdays, and the first delivery weekday. Per-delivery quantity comes from the
chosen :class:`~api.models.ProductType`. All of it is snapshotted onto the
schedule so the plan stays stable even if the rule/product later changes.

Creation is idempotent: if the enrollment already has delivery schedules it is a
no-op, so the verification-completion path can call it safely and repeatedly.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from api.models import (
    CadenceRule,
    DeliveryCadence,
    HouseholdMember,
    MemberDeliverySchedule,
    ProductType,
    ProductTypeKind,
    ScheduleStatus,
)
from api.services.catalog import menu_type_for_member, product_type_kind_for_name
from api.services.orders import _WEEKDAY_CODES, _delivery_dates

# Manual cadence -> the weekday codes deliveries land on. ``once_a_week`` carries
# no fixed day: the agent supplies the single weekday on the Logistics page.
CADENCE_WEEKDAYS = {
    DeliveryCadence.MON_THU: ["mon", "thu"],
    DeliveryCadence.TUE_FRI: ["tue", "fri"],
    DeliveryCadence.ONCE_A_WEEK: [],
}

# Boxes ship on a fixed weekly schedule that ignores the agent-picked cadence
# weekday: every Wednesday, with the purchase order cut the Friday before.
BOX_DELIVERY_WEEKDAY = "wed"
BOX_PO_WEEKDAY = "fri"


def cadence_options_for_kind(kind):
    """The delivery cadences available for a product ``kind`` (meals/boxes),
    each with the predefined per-delivery quantity from its ProductType row.

    Derived from the ProductType rows of that kind so the Logistics page only
    offers Meals cadences for a meals program and Boxes cadences for a boxes
    program (the two never mix). Falls back to every cadence (no default qty)
    when the kind is unknown or no ProductTypes are configured for it.
    """
    labels = dict(DeliveryCadence.choices)
    if kind is not None:
        opts, seen = [], set()
        rows = (
            ProductType.objects.filter(type=kind)
            .exclude(delivery_days_cadence="")
            .order_by("delivery_days_cadence")
        )
        for r in rows:
            if r.delivery_days_cadence in seen:
                continue
            seen.add(r.delivery_days_cadence)
            opts.append({
                "value": r.delivery_days_cadence,
                "label": labels.get(r.delivery_days_cadence, r.delivery_days_cadence),
                "prod_per_delivery": r.prod_per_delivery,
            })
        if opts:
            return opts
    return [
        {"value": v, "label": l, "prod_per_delivery": None}
        for v, l in DeliveryCadence.choices
    ]


def weekdays_for_cadence(cadence, once_a_week_weekday=None):
    """Resolve the delivery weekday codes for a manually chosen cadence. For
    ``once_a_week`` the agent-picked single weekday is used."""
    if cadence == DeliveryCadence.ONCE_A_WEEK:
        return [once_a_week_weekday] if once_a_week_weekday in _WEEKDAY_CODES else []
    return list(CADENCE_WEEKDAYS.get(cadence, []))


def _accept_date(case):
    """The date the case became active: its authorization approval start date,
    falling back to today when the case/date is missing."""
    starts_at = getattr(case, "service_authorization_approval_starts_at", None)
    if starts_at:
        return starts_at.date()
    return timezone.localdate()


def _window_end(case):
    """The authorization approval end date from the case, or None."""
    ends_at = getattr(case, "service_authorization_approval_ends_at", None)
    return ends_at.date() if ends_at else None


def _next_weekday(d, weekday):
    """The first date strictly after ``d`` whose weekday() == ``weekday``."""
    days = (weekday - d.weekday()) % 7
    return d + timedelta(days=days or 7)


def box_first_delivery(assignment_date):
    """First box delivery date for an assignment made on ``assignment_date``.

    Boxes are delivered every Wednesday and their purchase order is cut the
    Friday before. To make a Friday's PO batch the assignment must land BEFORE
    that Friday, so we take the next Friday strictly after the assignment date
    (assigning on Friday or the weekend rolls to the following Friday) and
    return the first Wednesday after it. In practice:

    * assigned Mon-Thu -> this week's Friday PO -> next week's Wednesday;
    * assigned Fri/Sat/Sun -> next week's Friday PO -> the Wednesday after that.
    """
    po_friday = _next_weekday(assignment_date, _WEEKDAY_CODES[BOX_PO_WEEKDAY])
    return _next_weekday(po_friday, _WEEKDAY_CODES[BOX_DELIVERY_WEEKDAY])


def cadence_rule_for(product_kind, accept_date):
    """The active CadenceRule for a product kind on the weekday a case is
    accepted, or None when no rule is configured."""
    if product_kind is None:
        return None
    return CadenceRule.objects.filter(
        product_kind=product_kind,
        accepted_weekday=accept_date.weekday(),
        is_active=True,
    ).first()


def _resolve_product_type(program_name, cadence):
    """Pick the ProductType for this plan: matched by program-name keyword
    (Meals/Boxes) and, when possible, the chosen weekday cadence. Falls back to
    any ProductType of the right kind, then None."""
    kind = product_type_kind_for_name(program_name)
    if kind is None:
        return None
    qs = ProductType.objects.filter(type=kind)
    if cadence:
        match = qs.filter(delivery_days_cadence=cadence).first()
        if match is not None:
            return match
    return qs.first()


@transaction.atomic
def create_member_delivery_schedules(
    enrollment, case=None, cadence="", once_a_week_weekday=None,
    kitchen=None, member_quantities=None,
):
    """Create one MemberDeliverySchedule per household member of ``enrollment``.

    Cadence is now chosen MANUALLY on the Logistics page (no CadenceRule
    auto-assignment). ``cadence`` is a :class:`~api.models.DeliveryCadence`
    value; for ``once_a_week`` the agent's single ``once_a_week_weekday`` code
    (mon/tue/...) is used. Delivery weekdays come straight from the cadence; the
    first delivery is the next matching weekday after the case activation date.

    ``kitchen`` is the household's assigned kitchen (snapshotted onto each plan).
    ``member_quantities`` optionally overrides per-member quantity, keyed by
    MemberDietaryProfile pk.

    ``case`` supplies the activation weekday + authorization window; falls back
    to ``enrollment.case``. Idempotent: returns ``[]`` if schedules already
    exist. When the authorization window is missing the total is 0 but the plans
    are still created.
    """
    if enrollment.delivery_schedules.exists():
        return []

    if case is None:
        case = enrollment.case

    members = list(
        enrollment.member_profiles.select_related("client").all()
    )
    if not members:
        return []

    program = case.program if case is not None else None
    program_name = (program.name if program is not None else "") or enrollment.program_name
    member_quantities = member_quantities or {}

    # Boxes ship on a fixed weekly schedule (Wednesdays, PO cut the Friday
    # before) that ignores the agent-picked weekday; meals use the chosen
    # cadence. Product type is still matched by program-kind AND cadence so
    # meals/boxes (and their per-delivery quantities) never mix.
    kind = product_type_kind_for_name(program_name)
    is_boxes = kind == ProductTypeKind.BOXES
    if is_boxes:
        delivery_weekdays = [BOX_DELIVERY_WEEKDAY]
    else:
        delivery_weekdays = weekdays_for_cadence(cadence, once_a_week_weekday)
    product_type = _resolve_product_type(program_name, cadence)

    # Persist the delivery weekdays onto the enrollment so any downstream order
    # generation expands the same days.
    if enrollment.delivery_weekdays != delivery_weekdays:
        enrollment.delivery_weekdays = delivery_weekdays
        enrollment.save(update_fields=["delivery_weekdays"])

    accept_date = _accept_date(case)
    end = _window_end(case)
    if is_boxes:
        # First box delivery is anchored on the assignment date (today): the
        # first Wednesday after the next PO Friday.
        start = box_first_delivery(timezone.localdate())
    else:
        # First meal delivery = the soonest chosen weekday strictly after the
        # accept day; the plan runs to the end of the auth window.
        candidates = [
            _next_weekday(accept_date, _WEEKDAY_CODES[w])
            for w in delivery_weekdays if w in _WEEKDAY_CODES
        ]
        start = min(candidates) if candidates else accept_date
    num_dates = len(_delivery_dates(start, end, delivery_weekdays))

    schedules = []
    for m in members:
        household_member = (
            HouseholdMember.objects.filter(client_id=m.client_id).first()
            if m.client_id
            else None
        )
        # Per-delivery quantity: explicit override > agent-entered member value
        # > the product type's default.
        prod_per_delivery = member_quantities.get(m.pk, m.meals_per_delivery)
        if prod_per_delivery is None:
            prod_per_delivery = product_type.prod_per_delivery if product_type else 0
        schedules.append(
            MemberDeliverySchedule(
                enrollment=enrollment,
                household_member=household_member,
                member_profile=m,
                member_name=m.member_name,
                program=program,
                product_type=product_type,
                kitchen=kitchen,
                delivery_days_cadence=cadence,
                prod_per_delivery=prod_per_delivery,
                meals_boxes_total=prod_per_delivery * num_dates,
                # Snapshot the member's menu type; derive it from their dietary
                # data as a fallback so the plan is never left without one.
                menu_type=m.menu_type or menu_type_for_member(
                    food_allergies=m.food_allergies, meal_category=m.meal_category,
                ),
                starts_on=start,
                ends_on=end,
                status=ScheduleStatus.SCHEDULED,
            )
        )
    return MemberDeliverySchedule.objects.bulk_create(schedules)


def current_household_cadence(enrollment):
    """The household's currently applied delivery cadence code, read from its
    first delivery schedule (the cadence is household-wide), or "" when none."""
    sched = enrollment.delivery_schedules.first()
    return sched.delivery_days_cadence if sched else ""


@transaction.atomic
def update_household_cadence(enrollment, cadence, once_a_week_weekday=None, case=None):
    """Re-apply a manually chosen cadence to a household that already has a
    delivery plan.

    Recomputes the delivery weekdays, first delivery date, per-delivery quantity
    (from the ProductType matching the program kind + cadence) and the window
    total, and writes them onto the enrollment and every existing
    MemberDeliverySchedule. Boxes keep their fixed Wednesday schedule (the
    cadence weekday is ignored, though the cadence value is still recorded).
    """
    if case is None:
        case = enrollment.case
    program = case.program if case is not None else None
    program_name = (program.name if program is not None else "") or enrollment.program_name
    kind = product_type_kind_for_name(program_name)
    is_boxes = kind == ProductTypeKind.BOXES

    if is_boxes:
        delivery_weekdays = [BOX_DELIVERY_WEEKDAY]
    else:
        delivery_weekdays = weekdays_for_cadence(cadence, once_a_week_weekday)
    product_type = _resolve_product_type(program_name, cadence)

    enrollment.delivery_weekdays = delivery_weekdays
    enrollment.save(update_fields=["delivery_weekdays"])

    accept_date = _accept_date(case)
    end = _window_end(case)
    if is_boxes:
        start = box_first_delivery(timezone.localdate())
    else:
        candidates = [
            _next_weekday(accept_date, _WEEKDAY_CODES[w])
            for w in delivery_weekdays if w in _WEEKDAY_CODES
        ]
        start = min(candidates) if candidates else accept_date
    num_dates = len(_delivery_dates(start, end, delivery_weekdays))

    for sched in enrollment.delivery_schedules.all():
        prod = product_type.prod_per_delivery if product_type else sched.prod_per_delivery
        sched.delivery_days_cadence = cadence
        if product_type is not None:
            sched.product_type = product_type
        sched.prod_per_delivery = prod
        sched.meals_boxes_total = (prod or 0) * num_dates
        sched.starts_on = start
        sched.ends_on = end
        sched.save(update_fields=[
            "delivery_days_cadence", "product_type", "prod_per_delivery",
            "meals_boxes_total", "starts_on", "ends_on",
        ])
    return enrollment

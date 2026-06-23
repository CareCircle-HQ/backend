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
    HouseholdMember,
    MemberDeliverySchedule,
    ProductType,
    ScheduleStatus,
)
from api.services.catalog import menu_type_for_member, product_type_kind_for_name
from api.services.orders import _delivery_dates


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
def create_member_delivery_schedules(enrollment, case=None):
    """Create one MemberDeliverySchedule per household member of ``enrollment``.

    ``case`` supplies the activation weekday (drives cadence) and the
    authorization window; it is passed in explicitly so this does NOT depend on
    the enrollment being linked to the case (a case maps to at most one
    enrollment). Falls back to ``enrollment.case`` when not provided.

    Idempotent: returns ``[]`` if schedules already exist. Returns the list of
    created rows otherwise. When the authorization window is missing the total
    is 0 but the plans are still created.
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

    # Cadence is decided by the weekday the case became active, via the editable
    # CadenceRule table. The agent does NOT pick delivery weekdays anymore — we
    # auto-select them (and the PO weekdays) from the matched rule.
    product_kind = product_type_kind_for_name(program_name)
    accept_date = _accept_date(case)
    rule = cadence_rule_for(product_kind, accept_date)

    cadence = rule.cadence if rule else ""
    delivery_weekdays = rule.delivery_weekdays if rule else []
    product_type = _resolve_product_type(program_name, cadence)

    # Persist the auto-selected delivery weekdays back onto the enrollment so the
    # downstream order generator expands the same days.
    if rule and enrollment.delivery_weekdays != delivery_weekdays:
        enrollment.delivery_weekdays = delivery_weekdays
        enrollment.save(update_fields=["delivery_weekdays"])

    # First delivery = the next delivery weekday strictly after the accept day
    # (the rule encodes edge cases like "accepted Wednesday -> first delivery
    # the following Monday"). The plan runs to the end of the auth window.
    end = _window_end(case)
    start = _next_weekday(accept_date, rule.first_delivery_weekday) if rule else accept_date
    num_dates = len(_delivery_dates(start, end, delivery_weekdays))

    schedules = []
    for m in members:
        household_member = (
            HouseholdMember.objects.filter(client_id=m.client_id).first()
            if m.client_id
            else None
        )
        # Per-delivery quantity: prefer the agent-entered member value, else the
        # product type's default.
        prod_per_delivery = m.meals_per_delivery
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

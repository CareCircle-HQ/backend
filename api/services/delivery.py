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
    MemberStatus,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
    ProductType,
    ProductTypeKind,
    ScheduleStatus,
)
from api.services.catalog import menu_type_for_member, product_type_kind_for_name
from api.services.orders import (
    _WEEKDAY_CODES,
    _delivery_dates,
    _weekday_ints,
    meals_for_delivery,
)

# Manual cadence -> the weekday codes deliveries land on. ``once_a_week`` carries
# no fixed day: the agent supplies the single weekday on the Logistics page.
CADENCE_WEEKDAYS = {
    DeliveryCadence.MON_THU: ["mon", "thu"],
    DeliveryCadence.TUE_FRI: ["tue", "fri"],
    DeliveryCadence.ONCE_A_WEEK: [],
}

# Boxes are ordered on a purchase order cut the Friday before delivery. The
# delivery weekday itself is now data-driven (per the kitchen's cadence): box
# kitchens can deliver on different days. Wednesday remains the default when a
# box cadence carries no fixed weekday.
BOX_DELIVERY_WEEKDAY = "wed"
BOX_PO_WEEKDAY = "fri"


def cadence_delivery_weekdays(cadence):
    """The fixed delivery weekday codes configured for a cadence, read from the
    Cadence settings table. Empty means a once-a-week style cadence where the
    agent picks the single delivery day at assignment time.

    Falls back to the legacy hardcoded map only when the Cadence table has no
    (active) row for the code, so an un-seeded environment still schedules.
    """
    from api.models import Cadence

    row = Cadence.objects.filter(code=cadence, is_active=True).first()
    if row is not None:
        return [w for w in (row.weekdays or []) if w in _WEEKDAY_CODES]
    if cadence == DeliveryCadence.ONCE_A_WEEK:
        return []
    return list(CADENCE_WEEKDAYS.get(cadence, []))


def cadence_options_for_kind(kind):
    """The delivery cadences a household of this product ``kind`` (meals/boxes)
    can be assigned, each with its delivery weekdays and the predefined
    per-delivery quantity.

    A cadence is offered when a kitchen that MAKES this product (``supported_
    products``) is linked to it (``Kitchen.cadences``) -- so the two never mix
    and the agent only sees cadences some capable kitchen actually runs. The
    per-delivery quantity comes from the matching ProductType row. When the
    kind is unknown, every active cadence is returned.
    """
    from api.models import Cadence, Kitchen, KitchenProductType

    active = list(Cadence.objects.filter(is_active=True))
    by_code = {c.code: c for c in active}

    qty = {}
    if kind is not None:
        for r in (
            ProductType.objects.filter(type=kind).exclude(delivery_days_cadence="")
        ):
            qty.setdefault(r.delivery_days_cadence, r.prod_per_delivery)

    product = {
        ProductTypeKind.MEALS: KitchenProductType.MEAL,
        ProductTypeKind.BOXES: KitchenProductType.BOX,
    }.get(kind)

    if product is None:
        chosen = active
    else:
        codes = set()
        # Filter supported_products in Python: the JSONField ``contains`` lookup
        # is unsupported on SQLite, so a DB-side filter breaks portability.
        for k in Kitchen.objects.prefetch_related("cadences"):
            if product not in (k.supported_products or []):
                continue
            codes.update(c.code for c in k.cadences.all() if c.is_active)
        chosen = [by_code[c] for c in codes if c in by_code]

    chosen.sort(key=lambda c: (c.label or c.code).lower())
    return [
        {
            "value": c.code,
            "label": c.label or c.code,
            "weekdays": [w for w in (c.weekdays or []) if w in _WEEKDAY_CODES],
            "prod_per_delivery": qty.get(c.code),
        }
        for c in chosen
    ]


def weekdays_for_cadence(cadence, once_a_week_weekday=None):
    """Resolve the delivery weekday codes for a chosen cadence. A cadence with no
    fixed weekdays (once-a-week style) uses the agent-picked single day."""
    fixed = cadence_delivery_weekdays(cadence)
    if fixed:
        return fixed
    return [once_a_week_weekday] if once_a_week_weekday in _WEEKDAY_CODES else []


def active_cadence_codes():
    """The set of active Cadence codes an assignment may use."""
    from api.models import Cadence

    return set(Cadence.objects.filter(is_active=True).values_list("code", flat=True))


def cadence_needs_weekday(cadence):
    """True when a cadence has no fixed delivery weekdays, so the agent must pick
    the single delivery day at assignment time (once-a-week style)."""
    return not cadence_delivery_weekdays(cadence)


def cadence_codes_for_kind(kind):
    """The set of active Cadence codes run by kitchens that make this product
    ``kind`` (meals/boxes). Empty when the kind is unknown."""
    from api.models import Kitchen, KitchenProductType

    product = {
        ProductTypeKind.MEALS: KitchenProductType.MEAL,
        ProductTypeKind.BOXES: KitchenProductType.BOX,
    }.get(kind)
    if product is None:
        return set()
    codes = set()
    # Filter the (small) supported_products JSON list in Python so this works on
    # every backend -- the JSONField ``contains`` lookup is unsupported on SQLite.
    for k in Kitchen.objects.prefetch_related("cadences"):
        if product not in (k.supported_products or []):
            continue
        codes.update(c.code for c in k.cadences.all() if c.is_active)
    return codes


def kitchen_cadence_for_delivery(kitchen, kind, delivery_weekday_code):
    """The cadence that governs a member's PO quantities for a delivery of
    ``kind`` on ``delivery_weekday_code``, resolved from the member's assigned
    ``kitchen`` (kitchen -> kind -> cadence -> quantities).

    Among the kitchen's active cadences it picks the one scoped to this product
    kind that delivers on the weekday. A cadence with fixed weekdays must include
    the delivery weekday; a once-a-week cadence (no fixed weekdays) matches any
    single delivery day. When several match, a fixed-weekday cadence is preferred
    over a once-a-week one (so a meals Mon/Thu cadence wins over a boxes
    once-a-week on a shared kitchen), then the lowest label for a deterministic
    result. Returns ``None`` when the kitchen is unset or nothing matches, so the
    caller falls back to legacy quantities.
    """
    if kitchen is None or delivery_weekday_code not in _WEEKDAY_CODES:
        return None
    kind_codes = cadence_codes_for_kind(kind)
    matches = []
    for c in kitchen.cadences.all():
        if not c.is_active:
            continue
        if kind_codes and c.code not in kind_codes:
            continue
        weekdays = [w for w in (c.weekdays or []) if w in _WEEKDAY_CODES]
        if weekdays and delivery_weekday_code not in weekdays:
            continue
        matches.append((0 if weekdays else 1, (c.label or c.code).lower(), c))
    matches.sort(key=lambda m: (m[0], m[1]))
    return matches[0][2] if matches else None


def cadence_po_weekday(kind, delivery_weekday_code):
    """The configured PO/cutoff weekday code for a delivery weekday under this
    product ``kind``, read from the Cadence settings table
    (``Cadence.po_weekdays``). Returns None when nothing is configured, so the
    caller falls back to its legacy default.

    Scoped to cadences run by kitchens of this kind so a meals delivery day and
    a boxes delivery day that share a weekday don't cross-configure each other.
    """
    from api.models import Cadence

    kind_codes = cadence_codes_for_kind(kind)
    for c in Cadence.objects.filter(is_active=True):
        if kind_codes and c.code not in kind_codes:
            continue
        po = (c.po_weekdays or {}).get(delivery_weekday_code)
        if po:
            return po
    return None


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


def _meal_delivery_anchor(case):
    """Anchor for the first MEAL delivery: the later of today and the auth
    window start.

    Anchoring on today means a case whose authorization start date is already in
    the past (common -- cases are often authorized with a start date already
    over) still schedules a real UPCOMING delivery instead of one in the past.
    When the auth window opens in the future we use that start instead, so we
    never schedule a delivery before the authorization is active.
    """
    return max(timezone.localdate(), _accept_date(case))


def _next_weekday(d, weekday):
    """The first date strictly after ``d`` whose weekday() == ``weekday``."""
    days = (weekday - d.weekday()) % 7
    return d + timedelta(days=days or 7)


def box_first_delivery(assignment_date, delivery_weekdays=None):
    """First box delivery date for an assignment made on ``assignment_date``.

    A box purchase order is cut the Friday before delivery, so the assignment
    must land BEFORE that Friday: we take the next Friday strictly after the
    assignment date (assigning on Friday or the weekend rolls to the following
    Friday) and return the first CONFIGURED delivery weekday after it. The
    delivery weekday(s) come from the kitchen's cadence, so box kitchens can
    deliver on different days; Wednesday is the default when none is configured.
    """
    weekdays = [w for w in (delivery_weekdays or []) if w in _WEEKDAY_CODES] or [
        BOX_DELIVERY_WEEKDAY
    ]
    po_friday = _next_weekday(assignment_date, _WEEKDAY_CODES[BOX_PO_WEEKDAY])
    return min(_next_weekday(po_friday, _WEEKDAY_CODES[w]) for w in weekdays)


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


def _resolve_product_type(program_name, cadence, kind=None):
    """Pick the ProductType for this plan: matched by program-name keyword
    (Meals/Boxes) and, when possible, the chosen weekday cadence. Falls back to
    any ProductType of the right kind, then None.

    ``kind`` overrides the program-name keyword detection when supplied."""
    kind = kind or product_type_kind_for_name(program_name)
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
    kitchen=None, member_quantities=None, product_kind=None,
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

    # Out-of-orbit members (allergy/menu combos the kitchen can't safely fulfill)
    # get no delivery plan and are therefore excluded from all Purchase Orders.
    members = list(
        enrollment.member_profiles.select_related("client")
        .exclude(status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .all()
    )
    if not members:
        return []

    program = case.program if case is not None else None
    program_name = (program.name if program is not None else "") or enrollment.program_name
    member_quantities = member_quantities or {}

    # Delivery weekdays come from the chosen cadence for meals AND boxes (box
    # kitchens can run different days); boxes still have their PO cut the Friday
    # before and default to Wednesday when the cadence carries no weekday.
    # Product type is still matched by program-kind AND cadence so meals/boxes
    # (and their per-delivery quantities) never mix.
    kind = product_kind or product_type_kind_for_name(program_name)
    is_boxes = kind == ProductTypeKind.BOXES
    delivery_weekdays = weekdays_for_cadence(cadence, once_a_week_weekday)
    if is_boxes and not delivery_weekdays:
        # Box cadence with no configured weekday defaults to Wednesday.
        delivery_weekdays = [BOX_DELIVERY_WEEKDAY]
    product_type = _resolve_product_type(program_name, cadence, kind=kind)

    # Persist the delivery weekdays onto the enrollment so any downstream order
    # generation expands the same days.
    if enrollment.delivery_weekdays != delivery_weekdays:
        enrollment.delivery_weekdays = delivery_weekdays
        enrollment.save(update_fields=["delivery_weekdays"])

    end = _window_end(case)
    if is_boxes:
        # First box delivery is anchored on the assignment date (today): the
        # first configured box weekday after the next PO Friday.
        start = box_first_delivery(timezone.localdate(), delivery_weekdays)
    else:
        # First meal delivery = the soonest chosen weekday strictly after the
        # anchor (the later of today and the auth window start), so a case
        # authorized with a start date already in the past still schedules a
        # real upcoming delivery. The plan runs to the end of the auth window.
        anchor = _meal_delivery_anchor(case)
        candidates = [
            _next_weekday(anchor, _WEEKDAY_CODES[w])
            for w in delivery_weekdays if w in _WEEKDAY_CODES
        ]
        start = min(candidates) if candidates else anchor
    delivery_dates = _delivery_dates(start, end, delivery_weekdays)
    num_dates = len(delivery_dates)
    weekday_ints = _weekday_ints(delivery_weekdays)
    # Meals use a per-day rate (per-delivery quantity varies by coverage); boxes
    # use a flat per-delivery count.
    meals_per_day = (
        product_type.meals_per_day if (product_type and not is_boxes) else 0
    )

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
        # Window total: meals sum each delivery's coverage x daily rate (9 + 12
        # + ...); boxes are a flat per-delivery count across all dates.
        if meals_per_day:
            total = sum(
                meals_for_delivery(d.weekday(), weekday_ints, meals_per_day)
                for d in delivery_dates
            )
        else:
            total = prod_per_delivery * num_dates
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
                meals_per_day=meals_per_day,
                meals_boxes_total=total,
                # Snapshot the member's menu type; derive it from their dietary
                # data as a fallback so the plan is never left without one.
                menu_type=m.menu_type or menu_type_for_member(
                    food_allergies=m.food_allergies, meal_category=m.meal_category,
                ),
                # Snapshot the meal-rule result sent to the kitchen on the PO.
                kitchen_meal_type=m.kitchen_meal_type,
                kitchen_food_notes=m.kitchen_food_notes,
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
def update_household_cadence(enrollment, cadence, once_a_week_weekday=None, case=None,
                            product_kind=None):
    """Re-apply a manually chosen cadence to a household that already has a
    delivery plan.

    Recomputes the delivery weekdays, first delivery date, per-delivery quantity
    (from the ProductType matching the program kind + cadence) and the window
    total, and writes them onto the enrollment and every existing
    MemberDeliverySchedule. Boxes keep their fixed Wednesday schedule (the
    cadence weekday is ignored, though the cadence value is still recorded).

    ``product_kind`` overrides the program-name keyword detection (which fails
    when a program name lacks a 'meal'/'box' keyword). Pass the robustly
    resolved kind (``product_kind_for_enrollment``) so weekdays are never
    computed against the wrong kind.
    """
    if case is None:
        case = enrollment.case
    program = case.program if case is not None else None
    program_name = (program.name if program is not None else "") or enrollment.program_name
    kind = product_kind or product_type_kind_for_name(program_name)
    is_boxes = kind == ProductTypeKind.BOXES

    delivery_weekdays = weekdays_for_cadence(cadence, once_a_week_weekday)
    if is_boxes and not delivery_weekdays:
        # Box cadence with no configured weekday defaults to Wednesday.
        delivery_weekdays = [BOX_DELIVERY_WEEKDAY]
    product_type = _resolve_product_type(program_name, cadence, kind=kind)

    enrollment.delivery_weekdays = delivery_weekdays
    enrollment.save(update_fields=["delivery_weekdays"])

    end = _window_end(case)
    if is_boxes:
        start = box_first_delivery(timezone.localdate(), delivery_weekdays)
    else:
        # Anchor the first meal delivery on the later of today and the auth
        # window start (see _meal_delivery_anchor) so a past-dated authorization
        # still suggests a real upcoming delivery.
        anchor = _meal_delivery_anchor(case)
        candidates = [
            _next_weekday(anchor, _WEEKDAY_CODES[w])
            for w in delivery_weekdays if w in _WEEKDAY_CODES
        ]
        start = min(candidates) if candidates else anchor
    delivery_dates = _delivery_dates(start, end, delivery_weekdays)
    num_dates = len(delivery_dates)
    weekday_ints = _weekday_ints(delivery_weekdays)
    # Meals use a per-day rate (per-delivery quantity varies by coverage); boxes
    # use a flat per-delivery count. Recompute the per-day rate too (0 for boxes)
    # so a meals<->boxes switch actually flips the plan's KIND snapshot --
    # plan_built_kind reads meals_per_day first, so leaving it stale kept the
    # plan on its old kind and the PO Blockers 'program_switched' fix never
    # cleared (the row remained after every fix). Mirrors the creation path.
    meals_per_day = (
        product_type.meals_per_day if (product_type and not is_boxes) else 0
    )
    meals_total = (
        sum(
            meals_for_delivery(d.weekday(), weekday_ints, meals_per_day)
            for d in delivery_dates
        )
        if meals_per_day else 0
    )

    for sched in enrollment.delivery_schedules.all():
        prod = product_type.prod_per_delivery if product_type else sched.prod_per_delivery
        sched.delivery_days_cadence = cadence
        if product_type is not None:
            sched.product_type = product_type
        sched.prod_per_delivery = prod
        sched.meals_per_day = meals_per_day
        sched.meals_boxes_total = meals_total if meals_per_day else (prod or 0) * num_dates
        sched.starts_on = start
        sched.ends_on = end
        sched.save(update_fields=[
            "delivery_days_cadence", "product_type", "prod_per_delivery",
            "meals_per_day", "meals_boxes_total", "starts_on", "ends_on",
        ])
    return enrollment


@transaction.atomic
def ensure_member_delivery_schedules(enrollment, case=None, product_kind=None):
    """Create a MemberDeliverySchedule for every household member that is MISSING
    one, snapshotting the household's ALREADY-CHOSEN cadence / kitchen / window.

    This closes the gap that hides members added to a household AFTER its first
    kitchen assignment: :func:`create_member_delivery_schedules` is a per-
    enrollment no-op once any plan exists, and every reconcile path
    (:func:`sync_delivery_calendar`, :func:`update_household_cadence`,
    ``recompute_delivery_plan``) only iterates EXISTING plans. So a newly added
    (and later activated) member never got a plan -- and, with no plan, never
    landed on the delivery calendar or any Purchase Order.

    Backs the manual "Rebuild calendar" action and the auto-heal that runs when a
    member is activated in an already-active household.

    No-op (returns ``[]``) when the household has no plan yet (nothing to
    snapshot from -- the first plan is created at kitchen assignment) or when
    every eligible member already has one. Out-of-orbit / paused / excluded
    members are skipped, exactly like the creation path, so an unserviceable
    member is not force-scheduled. Returns the created MemberDeliverySchedule
    rows.
    """
    cadence = current_household_cadence(enrollment)
    if not cadence:
        # No household plan exists yet -- the household hasn't been through
        # kitchen assignment, so there is nothing to extend.
        return []

    if case is None:
        # Prefer the governing authorization case (its window drives the plan),
        # falling back to the enrollment's own case.
        from api.services.lifecycle import governing_internal_case

        case = governing_internal_case(enrollment) or enrollment.case

    planned_profile_ids = set(
        enrollment.delivery_schedules.values_list("member_profile_id", flat=True)
    )
    members = list(
        enrollment.member_profiles.select_related("client")
        .exclude(status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .all()
    )
    missing = [m for m in members if m.pk not in planned_profile_ids]
    if not missing:
        return []

    program = case.program if case is not None else None
    program_name = (program.name if program is not None else "") or enrollment.program_name
    kind = product_kind
    if kind is None:
        from api.services.catalog import product_kind_for_enrollment

        kind = product_kind_for_enrollment(enrollment) or product_type_kind_for_name(
            program_name
        )
    is_boxes = kind == ProductTypeKind.BOXES

    # Preserve the agent-chosen single weekday for a once-a-week cadence.
    once_weekday = None
    if cadence == DeliveryCadence.ONCE_A_WEEK:
        wd = [w for w in (enrollment.delivery_weekdays or []) if w in _WEEKDAY_CODES]
        once_weekday = wd[0] if wd else None

    delivery_weekdays = weekdays_for_cadence(cadence, once_weekday)
    if is_boxes and not delivery_weekdays:
        delivery_weekdays = [BOX_DELIVERY_WEEKDAY]
    product_type = _resolve_product_type(program_name, cadence, kind=kind)
    kitchen = enrollment.kitchen  # household-level assignment

    end = _window_end(case)
    if is_boxes:
        start = box_first_delivery(timezone.localdate(), delivery_weekdays)
    else:
        anchor = _meal_delivery_anchor(case)
        candidates = [
            _next_weekday(anchor, _WEEKDAY_CODES[w])
            for w in delivery_weekdays if w in _WEEKDAY_CODES
        ]
        start = min(candidates) if candidates else anchor
    delivery_dates = _delivery_dates(start, end, delivery_weekdays)
    num_dates = len(delivery_dates)
    weekday_ints = _weekday_ints(delivery_weekdays)
    meals_per_day = (
        product_type.meals_per_day if (product_type and not is_boxes) else 0
    )

    schedules = []
    for m in missing:
        household_member = (
            HouseholdMember.objects.filter(client_id=m.client_id).first()
            if m.client_id
            else None
        )
        prod_per_delivery = m.meals_per_delivery
        if prod_per_delivery is None:
            prod_per_delivery = product_type.prod_per_delivery if product_type else 0
        if meals_per_day:
            total = sum(
                meals_for_delivery(d.weekday(), weekday_ints, meals_per_day)
                for d in delivery_dates
            )
        else:
            total = prod_per_delivery * num_dates
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
                meals_per_day=meals_per_day,
                meals_boxes_total=total,
                menu_type=m.menu_type or menu_type_for_member(
                    food_allergies=m.food_allergies, meal_category=m.meal_category,
                ),
                kitchen_meal_type=m.kitchen_meal_type,
                kitchen_food_notes=m.kitchen_food_notes,
                starts_on=start,
                ends_on=end,
                status=ScheduleStatus.SCHEDULED,
            )
        )
    return MemberDeliverySchedule.objects.bulk_create(schedules)

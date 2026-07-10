"""Purchase-order scheduling, preview, generation and splitting.

A PurchaseOrder is one batch for a single ``(product_type, delivery_date,
kitchen)``: it aggregates the dated delivery calendar (:class:`OrderSchedule`)
for that date and kitchen into per-member :class:`DeliveryOrder` lines.

Schedule (agreed rule, also stored on CadenceRule after migration 0084):
  - Meals deliver Mon/Thu (cadence ``mon_thu``) or Tue/Fri (``tue_fri``); each
    delivery weekday is ordered on its partner weekday (Mon PO -> Thu delivery,
    Thu PO -> next Mon; Tue PO -> Fri, Fri PO -> next Tue).
  - Boxes deliver Wednesday, ordered the Friday before.

Kitchen routing: a member's household-assigned kitchen is the DEFAULT (stored on
the OrderSchedule snapshot). At PO time an agent may reroute a member to another
capable kitchen for that date only; that is recorded on ``DeliveryOrder.kitchen``
(with ``default_kitchen`` + ``rerouted`` kept for the UI) and never mutates the
household preference.
"""
import csv
import io
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from api.models import (
    Address,
    AddressType,
    DeliveryOrder,
    DeliveryOrderStatus,
    DietaryRestriction,
    FoodAllergy,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MemberStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
    MenuType,
    OrderSchedule,
    ProductType,
    ProductTypeKind,
    PurchaseOrder,
    PurchaseOrderStatus,
    ScheduleStatus,
)
from api.services.catalog import product_kind_for_enrollment, product_type_kind_for_name
from api.services.kitchens import _MENU_CODE_TO_NAME, _norm
from api.services.orders import _WEEKDAY_CODES

_DIETARY_LABELS = dict(DietaryRestriction.choices)
_ALLERGY_LABELS = dict(FoodAllergy.choices)

# Reverse of _WEEKDAY_CODES: int weekday -> 3-letter code.
_WEEKDAY_NAMES = {v: k for k, v in _WEEKDAY_CODES.items()}
_WEEKDAY_ABBR = {0: "MON", 1: "TUE", 2: "WED", 3: "THU", 4: "FRI", 5: "SAT", 6: "SUN"}

# Meal delivery weekday (int) -> the PO weekday it is ordered on.
_MEAL_PO_WEEKDAY = {0: 3, 3: 0, 1: 4, 4: 1}  # Mon<-Thu, Thu<-Mon, Tue<-Fri, Fri<-Tue
# Box delivery is Wednesday, ordered the Friday before.
_BOX_PO_WEEKDAY = 4

# Meals per member per delivery, fixed by the delivery weekday: Mon/Tue = 9,
# Thu/Fri = 12 (21 meals/week/member across the two weekly deliveries). This is
# NOT taken from any per-member stored value. Boxes use the per-member scheduled
# quantity instead.
_MEAL_QTY_BY_WEEKDAY = {0: 9, 1: 9, 3: 12, 4: 12}


def meals_per_member_for_delivery(delivery_date):
    """Fixed meal count a member receives on this delivery weekday (0 when the
    weekday isn't a meal delivery day)."""
    return _MEAL_QTY_BY_WEEKDAY.get(delivery_date.weekday(), 0)


def delivery_quantity(kind, delivery_date, schedule):
    """Quantity for one member's delivery line. Meals are fixed by the delivery
    weekday; boxes use the member's scheduled quantity."""
    if kind == ProductTypeKind.MEALS:
        return meals_per_member_for_delivery(delivery_date)
    return schedule.how_many_meals_or_boxes or 0

# Delivery weekday (int) -> the DeliveryCadence the delivery belongs to.
_WEEKDAY_CADENCE = {
    0: "mon_thu", 3: "mon_thu",
    1: "tue_fri", 4: "tue_fri",
    2: "once_a_week",  # boxes
}


def _prev_weekday(d, weekday):
    """The first date strictly before ``d`` whose weekday() == ``weekday``."""
    days = (d.weekday() - weekday) % 7
    return d - timedelta(days=days or 7)


def po_date_for_delivery(kind, delivery_date):
    """The PO/cutoff date for a given product ``kind`` and delivery date.

    Reads the configurable PO cutoff weekday from the Cadence settings table
    (``Cadence.po_weekdays``); falls back to the legacy hardcoded map when a
    cadence doesn't declare one, so existing behavior is preserved.
    """
    from api.services.delivery import cadence_po_weekday

    wd = delivery_date.weekday()
    delivery_code = _WEEKDAY_NAMES.get(wd)
    po_code = cadence_po_weekday(kind, delivery_code) if delivery_code else None
    if po_code and po_code in _WEEKDAY_CODES:
        return _prev_weekday(delivery_date, _WEEKDAY_CODES[po_code])

    # Legacy fallback: hardcoded meal map / box Friday.
    if kind == ProductTypeKind.BOXES:
        po_wd = _BOX_PO_WEEKDAY
    else:
        po_wd = _MEAL_PO_WEEKDAY.get(wd)
    if po_wd is None:
        return None
    return _prev_weekday(delivery_date, po_wd)


def cadence_for_delivery_date(delivery_date):
    """The DeliveryCadence code a delivery on this weekday belongs to."""
    return _WEEKDAY_CADENCE.get(delivery_date.weekday(), "")


def cadences_for_delivery_date(kind, delivery_date):
    """Every active cadence (scoped to this product ``kind``) that delivers on
    ``delivery_date``'s weekday, each with its OWN PO/cutoff date.

    A single weekday can belong to more than one cadence (e.g. Tuesday is a
    delivery day for both ``tue_fri`` and ``tue_only``), and each cadence can be
    ordered on a different cutoff weekday -- so the PO popup must show them all
    rather than a single hardcoded cadence. Returns a list of
    ``{code, label, po_date}`` sorted by label.
    """
    from api.models import Cadence
    from api.services.delivery import cadence_codes_for_kind

    wd = delivery_date.weekday()
    delivery_code = _WEEKDAY_NAMES.get(wd)
    kind_codes = cadence_codes_for_kind(kind)
    out = []
    for c in Cadence.objects.filter(is_active=True):
        if kind_codes and c.code not in kind_codes:
            continue
        weekdays = [w for w in (c.weekdays or []) if w in _WEEKDAY_CODES]
        if weekdays and delivery_code not in weekdays:
            continue
        po_code = (c.po_weekdays or {}).get(delivery_code)
        if po_code and po_code in _WEEKDAY_CODES:
            po = _prev_weekday(delivery_date, _WEEKDAY_CODES[po_code])
        else:
            po = po_date_for_delivery(kind, delivery_date)
        out.append({
            "code": c.code,
            "label": c.label or c.code,
            "po_date": po.isoformat() if po else None,
        })
    out.sort(key=lambda x: (x["label"] or "").lower())
    return out


def _product_type_for(kind, delivery_date):
    """The ProductType matching the kind + the cadence implied by the weekday."""
    if kind is None:
        return None
    cadence = cadence_for_delivery_date(delivery_date)
    qs = ProductType.objects.filter(type=kind)
    if cadence:
        match = qs.filter(delivery_days_cadence=cadence).first()
        if match is not None:
            return match
    return qs.first()


def _kitchen_code(kitchen, _cache={}):
    """A short, stable code for a kitchen, e.g. "K01" (1-based by creation)."""
    if kitchen is None:
        return "K00"
    if not _cache:
        for i, kid in enumerate(
            Kitchen.objects.order_by("created_at").values_list("pk", flat=True)
        ):
            _cache[str(kid)] = "K%02d" % (i + 1)
    return _cache.get(str(kitchen.pk), "K%02d" % 0)


def build_po_number(kind, delivery_date, kitchen, split_seq=None):
    """Human-readable PO number, e.g. PO-MEALS-2026-W26-THU-K01 or
    PO-BOX-2026-W26-K01. ``split_seq`` (>=2) appends a "-S2" split suffix."""
    iso = delivery_date.isocalendar()
    year, week = iso[0], iso[1]
    kind_label = "BOX" if kind == ProductTypeKind.BOXES else "MEALS"
    parts = [f"PO-{kind_label}", str(year), f"W{week:02d}"]
    if kind != ProductTypeKind.BOXES:
        parts.append(_WEEKDAY_ABBR.get(delivery_date.weekday(), ""))
    parts.append(_kitchen_code(kitchen))
    number = "-".join(p for p in parts if p)
    if split_seq and split_seq >= 2:
        number = f"{number}-S{split_seq}"
    return number


def _ensure_unique_po_number(base):
    """Return ``base`` or a "-2", "-3"... suffixed variant that is unused."""
    if not PurchaseOrder.objects.filter(po_number=base).exists():
        return base
    i = 2
    while PurchaseOrder.objects.filter(po_number=f"{base}-{i}").exists():
        i += 1
    return f"{base}-{i}"


def _menu_type_index():
    """Normalized catalog MenuType name -> MenuType model instance."""
    return {_norm(mt.name): mt for mt in MenuType.objects.all()}


def _resolve_menu_type(code, index):
    """Map a member menu-type CODE to the catalog MenuType model, or None."""
    name = _MENU_CODE_TO_NAME.get(code, code)
    wanted = _norm(name)
    mt = index.get(wanted)
    if mt is not None:
        return mt
    return next(
        (v for key, v in index.items() if wanted and (wanted in key or key in wanted)),
        None,
    )


def _kitchen_offered_index():
    """kitchen_id (str) -> set of normalized MenuType names the kitchen offers."""
    idx = {}
    for k in Kitchen.objects.prefetch_related("kitchen_menu_types__menu_type"):
        idx[str(k.pk)] = {
            _norm(kmt.menu_type.name)
            for kmt in k.kitchen_menu_types.all()
            if kmt.menu_type_id
        }
    return idx


def _kitchen_supports_menu(kind, offered_norm, code):
    """True when this kitchen offers the member's menu type. Boxes don't map to
    KitchenMenuType rows, so they're always considered supported."""
    if kind == ProductTypeKind.BOXES:
        return True
    wanted = _norm(_MENU_CODE_TO_NAME.get(code, code))
    if not wanted:
        return False
    if wanted in offered_norm:
        return True
    return any(wanted in o or o in wanted for o in offered_norm)


def _dedupe_by_client(schedules):
    """Collapse multiple occurrences of the SAME client on one date to a single
    schedule.

    A client with two active enrollments (a data anomaly -- e.g. a spurious
    caseless duplicate alongside the real, case-linked one) builds two delivery
    calendars, so the same person can land twice on a date and get two lines in
    one PO. Keep one occurrence per client, preferring the one whose enrollment
    is linked to a case (the governing/legit enrollment); ``order_id`` is the
    stable final tiebreak so the choice is deterministic.
    """
    ordered = sorted(
        schedules,
        key=lambda s: (
            0 if getattr(s.enrollment, "case_id", None) else 1,
            str(s.order_id),
        ),
    )
    seen, out = set(), []
    for s in ordered:
        cid = s.member.client_id if s.member else None
        if cid is not None:
            if cid in seen:
                continue
            seen.add(cid)
        out.append(s)
    return out


def _due_schedules(kind, delivery_date):
    """SCHEDULED OrderSchedule rows for the given kind that land on the date.

    Schedules whose enrollment is On Hold or in a terminal stage
    (Service Complete / Closed / Cancelled) are excluded, as are Out of Orbit /
    Paused / Inactive members: none may appear in any new Purchase Order. Also
    de-duped per client so a duplicate-enrollment anomaly never doubles a line.
    """
    qs = (
        OrderSchedule.objects.filter(
            anticipated_delivery_date=delivery_date,
            status=ScheduleStatus.SCHEDULED,
        )
        .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        .exclude(member__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .select_related(
            "member", "member__client", "household", "kitchen",
            "enrollment", "enrollment__case", "enrollment__case__program",
        )
    )
    # The schedule's snapshot ``program_name`` doesn't always carry a meal/box
    # keyword (e.g. "Enhanced Care Management ..."), so fall back to the robust
    # per-enrollment resolver -- which reads the GOVERNING internal-service case
    # (the verification's case) -- before dropping a schedule. Cached per
    # enrollment to avoid re-resolving the same household.
    enr_kind = {}
    out = []
    for s in qs:
        schedule_kind = product_type_kind_for_name(s.program_name)
        if schedule_kind is None:
            if s.enrollment_id not in enr_kind:
                enr_kind[s.enrollment_id] = product_kind_for_enrollment(s.enrollment)
            schedule_kind = enr_kind[s.enrollment_id]
        if schedule_kind == kind:
            out.append(s)
    return _dedupe_by_client(out)


def _batched_client_ids(delivery_date):
    """Client ids that already have a LIVE DeliveryOrder for this delivery date.

    Cancelled delivery orders (e.g. from a cancelled PO) don't count -- those
    members are free to be batched again, so they stay selected by default in a
    new PO instead of being greyed out as "already ordered"."""
    return set(
        DeliveryOrder.objects.filter(expected_delivery_date=delivery_date)
        .exclude(member__isnull=True)
        .exclude(status=DeliveryOrderStatus.CANCELLED)
        .values_list("member_id", flat=True)
    )


def preview_purchase_orders(kind, delivery_date):
    """Aggregate the delivery calendar for ``(kind, delivery_date)`` grouped by
    each member's DEFAULT (household) kitchen, with menu-type counts, total
    meals, and remaining per-kitchen capacity (orders/day). Members already in a
    DeliveryOrder for that date are flagged ``batched``."""
    schedules = _due_schedules(kind, delivery_date)
    batched = _batched_client_ids(delivery_date)
    po_date = po_date_for_delivery(kind, delivery_date)

    # Existing delivery-order counts per kitchen for this date (for capacity).
    used = {}
    for kid in (
        DeliveryOrder.objects.filter(expected_delivery_date=delivery_date)
        .exclude(kitchen__isnull=True)
        .exclude(status=DeliveryOrderStatus.CANCELLED)
        .values_list("kitchen_id", flat=True)
    ):
        used[str(kid)] = used.get(str(kid), 0) + 1

    offered_idx = _kitchen_offered_index()

    groups = {}  # kitchen_id (str) or "" -> group dict
    for s in schedules:
        k = s.kitchen
        kid = str(k.pk) if k else ""
        g = groups.get(kid)
        if g is None:
            cap = k.max_orders_per_day if k else None
            g = groups[kid] = {
                "kitchen_id": kid or None,
                "kitchen_name": k.name if k else "Unassigned",
                "capacity": cap,
                "capacity_used": used.get(kid, 0),
                "capacity_left": (cap - used.get(kid, 0)) if cap is not None else None,
                "total_members": 0,
                "total_meals": 0,
                "unsupported_members": 0,
                "menu_types": {},
                "members": [],
                "_offered": offered_idx.get(kid, set()),
            }
        is_batched = s.member and s.member.client_id in batched
        qty = delivery_quantity(kind, delivery_date, s)
        code = s.menu_type or ""
        # A menu type only appears in the breakdown when this kitchen offers it
        # (meals only; boxes don't map to KitchenMenuType rows).
        supported = _kitchen_supports_menu(kind, g["_offered"], code)
        if supported:
            mt = g["menu_types"].setdefault(
                code, {"code": code, "label": _MENU_CODE_TO_NAME.get(code, code or "—"), "members": 0, "meals": 0}
            )
            mt["members"] += 1
            mt["meals"] += qty
        elif not is_batched:
            g["unsupported_members"] += 1
        if not is_batched:
            g["total_members"] += 1
            g["total_meals"] += qty
        g["members"].append({
            "schedule_id": str(s.order_id),
            "client_id": str(s.member.client_id) if s.member and s.member.client_id else None,
            "name": s.member_name or "",
            "menu_type": code,
            "menu_type_label": _MENU_CODE_TO_NAME.get(code, code or "—"),
            "meals": qty,
            "batched": bool(is_batched),
            "supported": bool(supported),
        })

    for g in groups.values():
        g["menu_types"] = list(g["menu_types"].values())
        g.pop("_offered", None)

    return {
        "kind": kind,
        "delivery_date": delivery_date.isoformat(),
        "po_date": po_date.isoformat() if po_date else None,
        "cadence": cadence_for_delivery_date(delivery_date),
        "cadences": cadences_for_delivery_date(kind, delivery_date),
        "kitchens": sorted(groups.values(), key=lambda x: (x["kitchen_name"] or "").lower()),
    }


@transaction.atomic
def generate_purchase_order(kind, delivery_date, kitchen, schedule_ids, split_seq=None):
    """Create one PurchaseOrder for ``kitchen`` on ``delivery_date`` and a
    DeliveryOrder for each selected OrderSchedule. Schedules whose default
    kitchen differs from ``kitchen`` are flagged ``rerouted``. Skips schedules
    already batched for that date (idempotent on re-submit)."""
    schedules = list(
        OrderSchedule.objects.filter(
            order_id__in=schedule_ids, status=ScheduleStatus.SCHEDULED
        )
        .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
        .exclude(member__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        .select_related("member", "member__client", "household", "kitchen")
    )
    already = _batched_client_ids(delivery_date)
    schedules = [
        s for s in schedules
        if not (s.member and s.member.client_id in already)
    ]
    # Collapse duplicate occurrences of the same client (two active enrollments
    # both building a calendar) so a client never gets two lines in one PO.
    schedules = _dedupe_by_client(schedules)
    if not schedules:
        return None

    po_number = _ensure_unique_po_number(
        build_po_number(kind, delivery_date, kitchen, split_seq=split_seq)
    )
    po = PurchaseOrder.objects.create(
        po_number=po_number,
        kind=kind or "",
        product_type=_product_type_for(kind, delivery_date),
        delivery_date=delivery_date,
        po_date=po_date_for_delivery(kind, delivery_date),
        kitchen=kitchen,
        status=PurchaseOrderStatus.DRAFT,
    )

    menu_index = _menu_type_index()
    orders = []
    for s in schedules:
        default_kitchen = s.kitchen
        rerouted = bool(kitchen and default_kitchen and kitchen.pk != default_kitchen.pk)
        orders.append(DeliveryOrder(
            purchase_order=po,
            member=s.member.client if s.member else None,
            group=s.household,
            status=DeliveryOrderStatus.PENDING,
            quantity=delivery_quantity(kind, delivery_date, s),
            expected_delivery_date=delivery_date,
            kitchen=kitchen,
            default_kitchen=default_kitchen,
            rerouted=rerouted,
            menu_type=_resolve_menu_type(s.menu_type, menu_index),
            # Meal-rule result is what the kitchen export actually sends.
            kitchen_meal_type=s.kitchen_meal_type,
            kitchen_food_notes=s.kitchen_food_notes,
        ))
    DeliveryOrder.objects.bulk_create(orders)
    return po


@transaction.atomic
def split_purchase_order(po, delivery_order_ids, new_delivery_date):
    """Move whole DeliveryOrders out of ``po`` into a new PurchaseOrder with its
    own delivery date. The new PO inherits kitchen/kind/product_type and links
    back via ``split_from``. Returns the new PO (or None if nothing moved)."""
    movers = list(
        po.delivery_orders.filter(delivery_order_id__in=delivery_order_ids)
    )
    if not movers:
        return None

    # Next split sequence for this kitchen+date lineage.
    root = po.split_from or po
    seq = root.splits.count() + 2  # -S2, -S3, ...
    po_number = _ensure_unique_po_number(
        build_po_number(po.kind or None, new_delivery_date, po.kitchen, split_seq=seq)
    )
    new_po = PurchaseOrder.objects.create(
        po_number=po_number,
        kind=po.kind,
        product_type=po.product_type,
        delivery_date=new_delivery_date,
        po_date=po_date_for_delivery(po.kind or None, new_delivery_date),
        kitchen=po.kitchen,
        delivery_company=po.delivery_company,
        status=PurchaseOrderStatus.DRAFT,
        split_from=root,
    )
    for do in movers:
        do.purchase_order = new_po
        do.expected_delivery_date = new_delivery_date
        do.save(update_fields=["purchase_order", "expected_delivery_date", "updated_at"])
    return new_po


# ---------------------------------------------------------------------------
# Kitchen export (CSV sent to the kitchen when a PO is dispatched)
# ---------------------------------------------------------------------------

# Boxes export columns, in order.
_BOX_EXPORT_HEADERS = [
    "Delivery Date", "OrderID", "HouseholdGroup", "PrimaryMemberID",
    "PrimaryHousehold", "Quantity", "MemberID", "Name",
    "Street Address", "Unit / Apt", "City", "State", "Zipcode",
    "Delivery Notes", "MenuType", "Allergies", "FOOD NOTE", "Email address", "Phone",
]


def _slug(value):
    """Filename-safe token: alnum + dashes, collapsed."""
    return re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-") or "NA"


def _member_address(client):
    """Best delivery address for a client: prefer DELIVERY, then CURRENT/HOME,
    then any. Returns an Address or None."""
    if client is None:
        return None
    addrs = list(client.addresses.all())
    if not addrs:
        return None
    by_type = {a.type: a for a in addrs}
    for t in (AddressType.DELIVERY, AddressType.CURRENT, AddressType.HOME):
        if t in by_type:
            return by_type[t]
    return addrs[0]


def _export_address(client):
    """The address to put on the kitchen export for a member.

    Source of truth is the member's active ``EnrollmentVerification.delivery_address``
    -- the exact record captured on the verification pop-up, shown/edited on the
    member profile Household tab, and used by the delivery-order serializer. Only
    when the enrollment has no delivery address do we fall back to the member's
    best standalone Address (legacy behavior), so we never regress to blank.
    """
    if client is None:
        return None
    # Lazy import to avoid a service <-> portal.serializers import cycle.
    from api.portal.serializers import active_enrollment

    enr = active_enrollment(client)
    if enr is not None and enr.delivery_address is not None:
        return enr.delivery_address
    return _member_address(client)


# Values that carry no dietary information and should be dropped from the note
# (codes or stored display labels), matched case-insensitively.
_NONE_LIKE = {"none", "no restrictions", "norestrictions", "n/a", "na"}


def _food_note(client):
    """Human-readable dietary note from the member's latest dietary profile:
    restrictions + allergies (labels) + free-text other restrictions. Drops
    "none"-like placeholders and de-duplicates while preserving order."""
    if client is None:
        return ""
    prof = (
        MemberDietaryProfile.objects.filter(client=client)
        .order_by("-updated_at")
        .first()
    )
    if prof is None:
        return ""
    parts = []
    seen = set()

    def add(label):
        text = (label or "").strip()
        key = text.lower()
        if not text or key in _NONE_LIKE or key in seen:
            return
        seen.add(key)
        parts.append(text)

    for code in (prof.dietary_restrictions or []):
        add(_DIETARY_LABELS.get(code, code))
    for code in (prof.food_allergies or []):
        add(_ALLERGY_LABELS.get(code, code))
    if (prof.other_dietary_restrictions or "").strip():
        add(prof.other_dietary_restrictions)
    return "; ".join(parts)


def _allergies_note(client):
    """Comma-joined verification food-allergy labels from the member's latest
    dietary profile. Drops "none"-like placeholders and de-duplicates while
    preserving order."""
    if client is None:
        return ""
    prof = (
        MemberDietaryProfile.objects.filter(client=client)
        .order_by("-updated_at")
        .first()
    )
    if prof is None:
        return ""
    parts = []
    seen = set()
    for code in (prof.food_allergies or []):
        label = (_ALLERGY_LABELS.get(code, code) or "").strip()
        key = label.lower()
        if not label or key in _NONE_LIKE or key in seen:
            continue
        seen.add(key)
        parts.append(label)
    return ", ".join(parts)


def kitchen_export_filename(po):
    """e.g. PO-BOX-2026-W27-K01_ENG.csv — includes PO number + kitchen."""
    po_part = _slug(po.po_number or str(po.pk))
    kitchen_part = _slug(po.kitchen.name if po.kitchen else "Unassigned")
    return f"{po_part}_{kitchen_part}.csv"


def _household_group_code(household):
    """Stable, unique-per-household grouping code for exports, e.g.
    ``HH-2C1AFF529738``. Deterministic from the household UUID, so every member
    of the same household shares the exact same code AND it stays identical
    across different PO exports (lets the recipient reconcile a household over
    time). 12 hex chars (48 bits) makes collisions effectively impossible.
    Empty when the member isn't in a household (no group to identify)."""
    if household is None:
        return ""
    return f"HH-{household.household_id.hex[:12].upper()}"


def _household_label(household):
    """Human-readable household identifier for exports: the household's name if
    set, otherwise a short ``HH-XXXXXXXX`` code derived from its UUID (members of
    the same household share this, so the kitchen can group their food).

    A trailing "Household" word (e.g. "NUTOVICS Household") is dropped so the
    export shows just the family name ("NUTOVICS")."""
    if household is None:
        return ""
    name = (household.name or "").strip()
    if name:
        name = re.sub(r"\s+household$", "", name, flags=re.IGNORECASE).strip()
        if name:
            return name
    return f"HH-{household.household_id.hex[:8].upper()}"


def _household_is_primary(client):
    """Whether this member is the PRIMARY (head) of their household -- the single
    ``True`` row per HouseholdGroup the kitchen can treat as the group's contact.
    A member with no household record is a lone recipient and so counts as their
    own primary (``True``)."""
    if client is None:
        return False
    hm = getattr(client, "household_membership", None)
    if hm is None:
        return True
    return bool(hm.is_primary)


def _delivery_notes(client):
    """Delivery notes captured on the verification pop-up for this member: the
    ``notes`` on their enrollment's delivery address. Falls back to any note on
    the member's own best address."""
    if client is None:
        return ""
    prof = (
        MemberDietaryProfile.objects.filter(client=client)
        .select_related("enrollment__delivery_address")
        .order_by("-updated_at")
        .first()
    )
    if prof and prof.enrollment_id and prof.enrollment.delivery_address:
        note = (prof.enrollment.delivery_address.notes or "").strip()
        if note:
            return note
    addr = _member_address(client)
    return (addr.notes or "").strip() if addr else ""


def _format_phone(value):
    """Format a US phone number as (XXX) XXX-XXXX. Leaves anything that isn't a
    plain 10-digit (or 1+10) number untouched."""
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw


def build_kitchen_export_rows(po):
    """Header + per-delivery-order rows for ``po``'s kitchen export."""
    # Group every household's members together (and sort households by the
    # HouseholdID label the kitchen reads) so the kitchen can prepare and pack
    # all of one household's food as a single batch. ``group_id`` is the
    # secondary key so members stay contiguous even when household names collide
    # or are blank.
    orders = list(
        po.delivery_orders.select_related(
            "member", "member__household_membership", "group", "menu_type"
        )
        .prefetch_related("member__addresses")
        .order_by("group__name", "group_id", "member__last_name", "member__first_name")
    )
    # Primary member's client id per household, repeated on every member row of
    # that household so the kitchen can tie a group to its head-of-household id.
    hh_ids = {do.group_id for do in orders if do.group_id}
    primary_by_hh = dict(
        HouseholdMember.objects.filter(household_id__in=hh_ids, is_primary=True)
        .values_list("household_id", "client_id")
    )

    def _primary_member_id(do, client):
        # Household: the group's primary client id (shared by all its members).
        # A member with no household -- or a household with no primary row -- is
        # its own primary, so fall back to the member's own id.
        if do.group_id:
            pid = primary_by_hh.get(do.group_id)
            if pid:
                return str(pid)
        return str(client.client_id) if client else ""

    rows = []
    for do in orders:
        c = do.member
        addr = _export_address(c)
        name = (
            f"{c.first_name} {c.last_name}".strip() if c else ""
        )
        rows.append([
            do.expected_delivery_date.isoformat() if do.expected_delivery_date else "",
            str(do.delivery_order_id),
            _household_group_code(do.group),
            _primary_member_id(do, c),
            _household_is_primary(c),
            do.quantity if do.quantity is not None else "",
            str(c.client_id) if c else "",
            name,
            addr.street if addr else "",
            addr.unit if addr else "",  # Address 2 = unit/apt (kept separate)
            addr.city if addr else "",
            addr.state if addr else "",
            addr.zip if addr else "",
            _delivery_notes(c),
            do.menu_type.name if do.menu_type else "",
            _allergies_note(c),
            _food_note(c),
            c.client_email_address if c else "",
            _format_phone(c.client_phone_number) if c else "",
        ])
    return _BOX_EXPORT_HEADERS, rows


def build_kitchen_export_csv(po):
    """Return ``(filename, csv_text)`` for ``po``'s kitchen export."""
    headers, rows = build_kitchen_export_rows(po)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return kitchen_export_filename(po), buf.getvalue()


# ---------------------------------------------------------------------------
# Per-order summary report (totals + per-menu-type breakdown)
# ---------------------------------------------------------------------------

def _report_filename(po):
    """e.g. PO-BOX-2026-W27-K01_ENG_summary.csv."""
    base = kitchen_export_filename(po)
    stem = base[:-4] if base.lower().endswith(".csv") else base
    return f"{stem}_summary.csv"


def _kitchen_menu_label(do):
    """The kitchen menu type for a delivery order (the meal-rule result sent to
    the kitchen), falling back to the system menu type, then a dash."""
    return (do.kitchen_meal_type or "").strip() or (
        do.menu_type.name if do.menu_type else "—"
    )


def build_po_summary_data(po):
    """Structured summary of a single PurchaseOrder for the report view:
    overall totals, a per-menu-type breakdown, and per-household member lines.
    "Quantity" is meals for meals POs and boxes for boxes POs (it reads the
    snapshotted ``DeliveryOrder.quantity``)."""
    orders = list(
        po.delivery_orders.select_related("member", "group", "menu_type")
        .order_by("group_id", "member__last_name", "member__first_name")
    )
    is_meals = (po.kind or "").lower() == ProductTypeKind.MEALS
    unit = "Meals" if is_meals else "Boxes"

    total_members = len(orders)
    total_qty = sum((do.quantity or 0) for do in orders)

    # Per-menu-type breakdown, keyed by the KITCHEN menu type (the meal-rule
    # result the kitchen actually cooks), not the system menu type. Falls back
    # to the system menu type when the kitchen snapshot is blank (e.g. boxes).
    by_menu = {}
    for do in orders:
        label = _kitchen_menu_label(do)
        agg = by_menu.setdefault(label, {"members": 0, "quantity": 0})
        agg["members"] += 1
        agg["quantity"] += (do.quantity or 0)
    menu_types = [
        {"label": label, "members": agg["members"], "quantity": agg["quantity"]}
        for label, agg in sorted(by_menu.items())
    ]

    # Per-household grouping.
    households = {}
    for do in orders:
        label = _household_label(do.group)
        h = households.setdefault(label, {"label": label, "quantity": 0, "members": []})
        c = do.member
        name = (f"{c.first_name} {c.last_name}".strip() if c else "") or "—"
        h["quantity"] += (do.quantity or 0)
        h["members"].append({
            "name": name,
            "quantity": do.quantity if do.quantity is not None else 0,
            "menu_type": _kitchen_menu_label(do),
        })

    return {
        "po_number": po.po_number or str(po.pk),
        "kind": (po.kind or "").lower(),
        "unit": unit,
        "kitchen_name": po.kitchen.name if po.kitchen else "Unassigned",
        "delivery_date": po.delivery_date.isoformat() if po.delivery_date else None,
        "total_members": total_members,
        "total_quantity": total_qty,
        "menu_types": menu_types,
        "households": sorted(households.values(), key=lambda h: h["label"]),
    }


def build_po_summary_report(po):
    """Return ``(filename, csv_text)`` for the PO summary report (CSV form)."""
    data = build_po_summary_data(po)
    unit = data["unit"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Purchase Order", data["po_number"]])
    writer.writerow(["Kind", unit])
    writer.writerow(["Kitchen", data["kitchen_name"]])
    writer.writerow(["Delivery Date", data["delivery_date"] or ""])
    writer.writerow([])
    writer.writerow(["Total Members", data["total_members"]])
    writer.writerow([f"Total {unit}", data["total_quantity"]])
    writer.writerow([])
    writer.writerow(["MenuType", "Members", unit])
    for mt in data["menu_types"]:
        writer.writerow([mt["label"], mt["members"], mt["quantity"]])

    return _report_filename(po), buf.getvalue()

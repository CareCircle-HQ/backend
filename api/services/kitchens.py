"""Kitchen capability matching for the Logistics / kitchen-assignment page.

A household is served by a single kitchen. A kitchen can serve a member when it
offers that member's menu type and does NOT list any of the member's food
allergies among the allergies it cannot accommodate
(``KitchenMenuType.restrictions``).

Two representations have to be reconciled:

* Member data uses choice CODES — menu type (``standard``/``fish_free``/…) and
  food allergies (``milk``/``fish``/…) on :class:`~api.models.MemberDietaryProfile`.
* Kitchens reference the catalog :class:`~api.models.MenuType` rows (by name) and
  allergy :class:`~api.models.DietaryTag` rows.

We bridge them by normalizing names and a small alias table, so the match stays
robust to label variations (e.g. "Milk" vs "Dairy").

Capability is advisory: callers can surface warnings but still assign a kitchen
that doesn't cover every member (per product requirements).
"""
import re

from api.models import (
    FoodAllergy,
    Kitchen,
    KitchenProductType,
    KitchenStatus,
    ProductTypeKind,
)
from api.services.catalog import (
    product_kind_for_enrollment,
    product_type_kind_for_name,
)

# Member menu-type CODE -> the catalog MenuType.name we expect to match against.
# Hardcoded because the member-level ``MenuType`` TextChoices is shadowed in
# api.models by the later ``MenuType`` model (which has no ``.choices``).
_MENU_CODE_TO_NAME = {
    "standard": "Standard",
    "fish_free": "Fish Free",
    "vegetarian": "Vegetarian",
    "dairy_free": "Dairy Free",
}

# Member food-allergy CODE -> normalized name tokens that identify the matching
# allergy DietaryTag(s). A kitchen restriction tag blocks the member when its
# normalized name contains any of these tokens.
_ALLERGY_ALIASES = {
    "soy": ("soy",),
    "wheat": ("wheat", "gluten"),
    "sesame": ("sesame",),
    "red_meat": ("red meat", "beef"),
    "pork": ("pork",),
    "milk": ("milk", "dairy"),
    "eggs": ("egg",),
    "fish": ("fish",),
    "shellfish": ("shellfish", "shrimp", "crab"),
    "tree_nuts": ("tree nut", "nut"),
    "peanuts": ("peanut",),
}

# ProductTypeKind (meals/boxes) -> KitchenProductType (meal/box).
_KIND_TO_PRODUCT = {
    ProductTypeKind.MEALS: KitchenProductType.MEAL,
    ProductTypeKind.BOXES: KitchenProductType.BOX,
}

_ALLERGY_LABELS = dict(FoodAllergy.choices)


def _norm(value):
    """Lowercase and collapse non-alphanumerics to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _allergy_blocked_by(allergy_code, restriction_tag_names_norm):
    """True when ``allergy_code`` matches any normalized restriction tag name."""
    for alias in _ALLERGY_ALIASES.get(allergy_code, (allergy_code.replace("_", " "),)):
        alias_n = _norm(alias)
        if any(alias_n and alias_n in name for name in restriction_tag_names_norm):
            return True
    return False


def _member_allergy_codes(profile):
    """Real food-allergy codes for a member (drops 'none'/'other')."""
    return [
        c for c in (profile.food_allergies or [])
        if c and c not in ("none", "other")
    ]


def kitchen_offered_menu_index(kitchen):
    """Index a kitchen's offered menu types by normalized catalog name."""
    return {_norm(kmt.menu_type.name): kmt for kmt in kitchen.kitchen_menu_types.all()}


def member_coverage_for_kitchen(profile, kitchen, offered=None):
    """Whether ``kitchen`` can serve ``profile`` based on the member's menu type
    and food allergies vs. the kitchen's offered menus and their restrictions.

    Returns ``(covered: bool, reason: str, price)``. Does NOT consider the
    kitchen's product kind or active status -- callers layer those on.
    """
    if offered is None:
        offered = kitchen_offered_menu_index(kitchen)
    menu_label = _MENU_CODE_TO_NAME.get(profile.menu_type, profile.menu_type)
    wanted = _norm(menu_label)
    kmt = offered.get(wanted)
    if kmt is None:
        # Looser contains-match on the menu name.
        kmt = next(
            (v for key, v in offered.items() if wanted and (wanted in key or key in wanted)),
            None,
        )
    if kmt is None:
        return False, f"No {menu_label or 'matching'} menu", None
    restriction_names = [_norm(t.name) for t in kmt.restrictions.all()]
    blocked = [
        _ALLERGY_LABELS.get(c, c)
        for c in _member_allergy_codes(profile)
        if _allergy_blocked_by(c, restriction_names)
    ]
    if blocked:
        return False, f"Can't handle: {', '.join(blocked)}", kmt.menu_type_price
    return True, "", kmt.menu_type_price


def required_product_for_program(program_name):
    """Map a program name to the ``KitchenProductType`` (meal/box) a kitchen must
    make to serve it, or ``None`` when it can't be determined."""
    kind = product_type_kind_for_name(program_name or "")
    return _KIND_TO_PRODUCT.get(kind)


def serving_kitchens_for_member(profile, kitchens=None, *, required_product=None, active_only=True):
    """List the kitchens that can serve ``profile`` (menu type + allergies).

    Optionally require the kitchen to make a given product kind
    (``required_product``, a ``KitchenProductType``) and/or be ACTIVE. Returns a
    list of ``{"kitchen", "price"}`` dicts, in the kitchens' given order.
    """
    if kitchens is None:
        kitchens = (
            Kitchen.objects.all()
            .prefetch_related("kitchen_menu_types__menu_type", "kitchen_menu_types__restrictions")
            .order_by("name")
        )
    serving = []
    for k in kitchens:
        if active_only and k.status != KitchenStatus.ACTIVE:
            continue
        if required_product is not None and required_product not in (k.supported_products or []):
            continue
        covered, _reason, price = member_coverage_for_kitchen(profile, k)
        if covered:
            serving.append({"kitchen": k, "price": price})
    return serving


def _member_payload(profile):
    return {
        "member_id": profile.pk,
        "client_id": str(profile.client_id) if profile.client_id else None,
        "name": profile.member_name or "",
        "status": profile.status,
        "status_label": profile.get_status_display(),
        "menu_type": profile.menu_type,
        "menu_type_label": _MENU_CODE_TO_NAME.get(profile.menu_type, profile.menu_type or ""),
        "dietary_restrictions": list(profile.dietary_restrictions or []),
        "food_allergies": [
            {"code": c, "label": _ALLERGY_LABELS.get(c, c)}
            for c in (profile.food_allergies or [])
            if c and c != "none"
        ],
        "meals_per_delivery": profile.meals_per_delivery,
        "other_dietary_restrictions": profile.other_dietary_restrictions or "",
        "verification_notes": profile.general_verification_notes or "",
    }


def kitchen_options(enrollment):
    """Build the kitchen-assignment payload for one enrollment: the product kind,
    the household members (read-only dietary), and every active kitchen with a
    per-member coverage breakdown + warnings.

    A kitchen is ``eligible`` when it supports the product kind and covers every
    member (offers their menu type with none of their allergies blocked). The
    UI may still allow assigning an ineligible kitchen.
    """
    members = list(enrollment.member_profiles.select_related("client").all())
    member_payloads = [_member_payload(m) for m in members]

    kind = product_kind_for_enrollment(enrollment)
    required_product = _KIND_TO_PRODUCT.get(kind)

    kitchens = (
        Kitchen.objects.all()
        .prefetch_related("kitchen_menu_types__menu_type", "kitchen_menu_types__restrictions")
        .order_by("name")
    )

    results = []
    for k in kitchens:
        # Index this kitchen's offered menu types by normalized name.
        offered = kitchen_offered_menu_index(k)

        supports_product = (
            required_product is None
            or required_product in (k.supported_products or [])
        )

        coverage = []
        warnings = []
        for m, payload in zip(members, member_payloads):
            covered, reason, price = member_coverage_for_kitchen(m, k, offered=offered)
            if not covered:
                warnings.append(f"{payload['name'] or 'Member'}: {reason}")
            coverage.append({
                "member_id": m.pk,
                "name": payload["name"],
                "covered": covered,
                "reason": reason,
                "price": price,
            })

        eligible = supports_product and all(c["covered"] for c in coverage)
        if not supports_product and kind is not None:
            warnings.insert(0, f"Doesn't make {kind}")
        if k.status != KitchenStatus.ACTIVE:
            warnings.insert(0, f"Kitchen is {k.get_status_display().lower()}")

        results.append({
            "id": str(k.pk),
            "name": k.name,
            "address": k.address,
            "status": k.status,
            "status_label": k.get_status_display(),
            "supported_products": list(k.supported_products or []),
            "supports_product": supports_product,
            "eligible": eligible,
            "coverage": coverage,
            "warnings": warnings,
        })

    # Eligible kitchens first, then by name.
    results.sort(key=lambda r: (not r["eligible"], r["name"].lower()))

    return {
        "product_kind": kind,
        "members": member_payloads,
        "kitchens": results,
    }

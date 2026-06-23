"""Build up the master/lookup catalog (ProgramMainCategory -> Program ->
Service) from saved Screening, Assessment, and Case records.

These helpers are deduped (so each unique value is stored once) and are called
best-effort from the serializers: a catalog hiccup must never break the
underlying client/case/screening save, so callers wrap them in try/except.
"""

import logging
import re
import uuid

from django.utils.text import slugify

from api.models import (
    Program,
    ProgramMainCategory,
    ProductType,
    ProductTypeKind,
    Service,
)

logger = logging.getLogger(__name__)

# Member-level menu type codes. NOTE: the member-level ``MenuType`` TextChoices
# (api.models, "standard"/"fish_free"/"vegetarian"/"dairy_free") is shadowed in
# the models namespace by the later ``MenuType`` model, so we reference the
# stable string codes (the values stored on MemberDietaryProfile.menu_type)
# directly rather than importing the enum.
_MENU_STANDARD = "standard"
_MENU_FISH_FREE = "fish_free"
_MENU_VEGETARIAN = "vegetarian"
_MENU_DAIRY_FREE = "dairy_free"

# Allergies that force a specific (stricter) menu type, highest priority. Menu
# type is single-select, so the first match in this order wins.
_ALLERGY_MENU = {
    "milk": _MENU_DAIRY_FREE,
    "fish": _MENU_FISH_FREE,
    "shellfish": _MENU_FISH_FREE,
}
# Member meal_category -> menu type (near 1:1 mapping).
_CATEGORY_MENU = {
    "fresh_meal": _MENU_STANDARD,
    "dairy_free": _MENU_DAIRY_FREE,
    "fish_free": _MENU_FISH_FREE,
    "vegetarian": _MENU_VEGETARIAN,
}


def menu_type_for_member(food_allergies=None, meal_category=""):
    """Derive a member's menu type code from their dietary data.

    Precedence: a food allergy that maps to a stricter menu (milk -> Dairy Free,
    fish/shellfish -> Fish Free) wins; otherwise the member's ``meal_category``
    maps to the matching menu; default Standard. Menu type is single-select and
    cannot represent combinations, so the first matching allergy (in
    ``_ALLERGY_MENU`` order) is used.
    """
    allergies = {(a or "").strip().lower() for a in (food_allergies or [])}
    for code, menu in _ALLERGY_MENU.items():
        if code in allergies:
            return menu
    return _CATEGORY_MENU.get((meal_category or "").strip().lower(), _MENU_STANDARD)


# Matches a trailing parenthetical main category, e.g.
# "Clinically Appropriate Meals (Food)" -> ("Clinically Appropriate Meals", "Food").
_CATEGORY_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<category>[^()]+)\)\s*$")


def _clean(value):
    """Normalize a catalog value that may be a plain string or a dict with a
    ``name``/``code`` key into a trimmed string."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("code") or ""
    return (value or "").strip() if isinstance(value, str) else ""


def _split_name_and_category(name):
    """Split a program label into (program_name, main_category_or_None).

    The screening program name usually embeds its main category in a trailing
    parenthetical, e.g. "Clinically Appropriate Meals (Food)" means the program
    belongs to the "Food" main category. If we can't extract one, the category
    is ``None`` and the program is left without a relationship.
    """
    match = _CATEGORY_RE.match(name)
    if not match:
        return name, None
    base = match.group("name").strip()
    category = match.group("category").strip()
    if not base or not category:
        return name, None
    return base, category


def upsert_main_categories(names):
    """Store unique ProgramMainCategory rows from Screening results."""
    for raw in names or []:
        name = _clean(raw)
        if name:
            ProgramMainCategory.objects.get_or_create(name=name)


def upsert_program(name):
    """Get-or-create a master Program by name (auto-UUID for new rows).

    If the name carries a trailing "(Main Category)" the parenthetical is parsed
    out, stored as a ProgramMainCategory, and linked to the program. When no
    category can be extracted the program is stored without a relationship.
    """
    name = _clean(name)
    if not name:
        return None
    program_name, category_name = _split_name_and_category(name)
    # Tolerate pre-existing duplicate Programs with the same name (the name
    # column isn't unique): pick the first rather than letting get_or_create
    # raise MultipleObjectsReturned.
    program = Program.objects.filter(name=program_name).order_by("pk").first()
    if program is None:
        program = Program.objects.create(
            name=program_name, program_id=uuid.uuid4()
        )
    if category_name:
        category, _ = ProgramMainCategory.objects.get_or_create(name=category_name)
        if program.main_category_id != category.pk:
            program.main_category = category
            program.save(update_fields=["main_category"])
    return program


def upsert_programs(names):
    """Store unique Programs from an Assessment's eligible_services."""
    for raw in names or []:
        upsert_program(raw)


def product_type_kind_for_name(program_name):
    """Map a program name to a ProductTypeKind by keyword: 'meals' -> Meals,
    'box'/'boxes' -> Boxes. Returns None when neither keyword is present."""
    name = (program_name or "").casefold()
    if "meals" in name or "meal" in name:
        return ProductTypeKind.MEALS
    if "boxes" in name or "box" in name:
        return ProductTypeKind.BOXES
    return None


def assign_product_type_for_internal_service(program):
    """Link an Internal Service program to the right ProductType (Meals/Boxes)
    based on a keyword in its name. No-op when the program is None, the name has
    no matching keyword, or the matching ProductType row doesn't exist.

    Callers should only invoke this for programs on Internal Service cases.
    """
    if program is None:
        return None
    kind = product_type_kind_for_name(program.name)
    if kind is None:
        return None
    product_type = ProductType.objects.filter(type=kind).first()
    if product_type is None:
        return None
    if program.product_type_id != product_type.pk:
        program.product_type = product_type
        program.save(update_fields=["product_type"])
    return product_type


def _unique_service_code(name):
    """Generate a unique slug code for a new Service (code is unique/required)."""
    base = (slugify(name) or uuid.uuid4().hex)[:80]
    code = base
    suffix = 1
    while Service.objects.filter(code=code).exists():
        tail = f"-{suffix}"
        code = base[: 80 - len(tail)] + tail
        suffix += 1
    return code


def upsert_service_from_case(service_type, program_name):
    """Store a unique Service (by name) from a Case's service_type and link it
    to the master Program identified by the case's program_name."""
    name = _clean(service_type)
    if not name:
        return None
    program = upsert_program(program_name)
    service = Service.objects.filter(name=name).first()
    if service is None:
        service = Service.objects.create(code=_unique_service_code(name), name=name)
    if program and service.program_id != program.pk:
        service.program = program
        service.save(update_fields=["program"])
    return service

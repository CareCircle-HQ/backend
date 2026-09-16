"""Build up the master/lookup catalog (ProgramMainCategory -> Program ->
Service) from saved Screening, Assessment, and Case records.

These helpers are deduped (so each unique value is stored once) and are called
best-effort from the serializers: a catalog hiccup must never break the
underlying client/case/screening save, so callers wrap them in try/except.
"""

import logging
import re
import uuid
from functools import lru_cache

from django.utils.text import slugify

from api.models import (
    Program,
    ProgramMainCategory,
    ProductType,
    ProductTypeKind,
    Service,
)

logger = logging.getLogger(__name__)

# Programs are organization-scoped and opt-in: the master list only ever GAINS
# programs for the primary provider below. Cases from other providers, and the
# name-based paths (assessment eligibility / service catalog) never create new
# Program rows -- they only link to a program that already exists. Existing rows
# are never removed by this rule.
ALLOWED_PROGRAM_PROVIDER_NAME = "Met Council - SCN - PHS"


def is_allowed_program_provider(provider):
    """True when ``provider`` is the org whose programs we add. Accepts a
    Provider instance or a name string; None/other providers return False."""
    name = getattr(provider, "name", provider) or ""
    return str(name).strip() == ALLOWED_PROGRAM_PROVIDER_NAME


# Member-level menu type NAMES. ``MemberDietaryProfile.menu_type`` stores the
# admin-managed catalog ``MenuType`` name (e.g. "Standard", "Kosher"), so the
# derived fallback below returns names too.
_MENU_STANDARD = "Standard"
_MENU_FISH_FREE = "Fish Free"
_MENU_VEGETARIAN = "Vegetarian"
_MENU_DAIRY_FREE = "Dairy Free"

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
        # Programs are only ADDED via the Case sync path, and only for the
        # allowed organization (Met Council - SCN - PHS). This provider-less,
        # name-based path (assessment eligibility / service catalog) no longer
        # creates rows -- it only links to a program that already exists.
        return None
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
    """Map a program name to a ProductTypeKind by keyword.

    Meals: any 'meal'/'meals'. Boxes: the box family, which in the Met Council
    program names appears as 'box'/'boxes' but ALSO as 'voucher' / 'produce
    prescription' / 'food prescription' / 'pantry' / 'groceries' (all the
    Produce Prescription/Voucher product, e.g. "...Food Prescriptions: Voucher
    - ..."). Returns None when nothing matches. Meals is checked first; box
    programs never contain 'meal'."""
    name = (program_name or "").casefold()
    if "meal" in name:
        return ProductTypeKind.MEALS
    if any(
        kw in name
        for kw in (
            "box", "voucher", "produce prescription",
            "food prescription", "pantry", "groceries",
        )
    ):
        return ProductTypeKind.BOXES
    return None


def product_kind_for_enrollment(enrollment):
    """Resolve the Meals/Boxes product kind for an enrollment as robustly as
    possible.

    A program NAME alone doesn't always contain a 'meal'/'box' keyword (notably
    on production data), which previously left the kind unresolved (``None``) —
    surfacing as a "—" service label and a mixed meals+boxes cadence list. This
    tries, in order:

      1. The linked Program's ProductType.type (authoritative when set).
      2. The keyword heuristic on the case program / enrollment program name.
      3. The kind of any existing delivery schedule's ProductType (the household
         already has a plan, so its product is known).

    Returns a ProductTypeKind value, or None only when it truly can't be
    determined.
    """
    if enrollment is None:
        return None
    # 0. A manual per-household override wins over every derived signal. An agent
    #    sets this on the Household tab to correct a misclassified kind.
    override = getattr(enrollment, "product_type_override", None)
    if override is not None:
        coerced = _coerce_product_kind(override.type)
        if coerced is not None:
            return coerced
    # The authoritative program lives on the GOVERNING internal-service case (the
    # verification's case), which is often not the same row as ``enrollment.case``
    # (frequently null). Prefer it so the kind still resolves when the
    # enrollment's snapshot program name lacks a meal/box keyword. Lazy import
    # avoids a circular dependency with api.services.lifecycle.
    case = getattr(enrollment, "case", None)
    try:
        from api.services.lifecycle import governing_internal_case

        gov = governing_internal_case(enrollment)
    except Exception:
        gov = None
    if gov is not None:
        case = gov
    program = case.program if (case is not None and getattr(case, "program_id", None)) else None
    # 1. Program -> ProductType link (set for Internal Service programs).
    if program is not None and getattr(program, "product_type_id", None):
        pt = program.product_type
        coerced = _coerce_product_kind(pt.type) if pt is not None else None
        if coerced is not None:
            return coerced
    # 2. Keyword heuristic across every available name: the linked Program row,
    #    the enrollment's snapshot name, and the case's own program/service
    #    fields. The Program row name can be stripped of the meal/box keyword
    #    (e.g. main-category parsing), so we must not stop at it — the richer
    #    snapshot/case names often still carry it.
    for candidate in (
        program.name if program is not None else "",
        enrollment.program_name,
        getattr(case, "program_name", "") if case is not None else "",
        getattr(case, "service_type", "") if case is not None else "",
    ):
        kind = product_type_kind_for_name(candidate)
        if kind:
            return kind
    # 3. Fall back to the product of any existing delivery schedule.
    sched = enrollment.delivery_schedules.filter(product_type__isnull=False).first()
    if sched is not None and sched.product_type:
        return _coerce_product_kind(sched.product_type.type)
    return None


def detected_product_kind_for_enrollment(enrollment):
    """The meals/boxes kind DETECTED from names (the program-name keyword) for
    the governing internal-service case, IGNORING any manual override and the
    Program->ProductType link. This is the same signal ``assign_product_type_for
    _internal_service`` uses on case save, and is what the Household tab compares
    the effective kind against to flag a misclassification. Returns a
    ProductTypeKind, or None when no keyword is present."""
    if enrollment is None:
        return None
    case = getattr(enrollment, "case", None)
    try:
        from api.services.lifecycle import governing_internal_case

        gov = governing_internal_case(enrollment)
    except Exception:
        gov = None
    if gov is not None:
        case = gov
    program = case.program if (case is not None and getattr(case, "program_id", None)) else None
    # Only names belonging to the GOVERNING CASE -- deliberately NOT
    # ``enrollment.program_name``. The enrollment snapshot reflects the kind the
    # household was VERIFIED under, so including it here would make the governing
    # baseline echo the verified kind and hide a genuine case mismatch (e.g. a
    # meals governing case with a boxes-verified enrollment).
    for candidate in (
        program.name if program is not None else "",
        getattr(case, "program_name", "") if case is not None else "",
        getattr(case, "service_type", "") if case is not None else "",
    ):
        kind = product_type_kind_for_name(candidate)
        if kind:
            return kind
    return None


def _coerce_product_kind(value):
    """Coerce a raw product-type string into a ProductTypeKind, or None."""
    try:
        return ProductTypeKind(value)
    except ValueError:
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


# ── Service TYPE (domain) of a case ───────────────────────────────────────────
# Internal Services is no longer food-only: housing programs (Dwelling Assessment
# / SOW Development, Home Remediation) are internal services of a different TYPE.
# The rule is ONE GOVERNING INTERNAL-SERVICE CASE PER TYPE, so a member can hold a
# Food case and a Housing case at once without them competing.
#
# A case carries no type column -- it is derived from ``program_name`` via the
# ActiveProgram table, which is the same place `derive_case_type_from_active_program`
# reads the case CATEGORY from. That table is small (~324 rows) and changes rarely,
# so the lookup is an in-process cached dict rather than a query per call: governing
# -case resolution runs at hundreds of sites, including the 76k-row analytics
# rebuild. The cache is cleared whenever an ActiveProgram row is saved or deleted
# (see api/apps.py).

FOOD_DOMAIN = "food"


@lru_cache(maxsize=1)
def _program_domain_map():
    """``{program_name.casefold(): case_type}`` for every ActiveProgram row."""
    from api.models import ActiveProgram

    return {
        (name or "").strip().casefold(): (ctype or "")
        for name, ctype in ActiveProgram.objects.values_list(
            "program_name", "case_type",
        )
        if name
    }


def clear_program_domain_cache():
    """Drop every cached ActiveProgram-derived map (a row changed).

    One entry point on purpose: the post_save signal in api/apps.py calls this,
    and a second cache added later must be cleared here rather than needing the
    signal to know about it.
    """
    _program_domain_map.cache_clear()
    _program_service_type_map.cache_clear()


def program_service_domain(program_name):
    """The service TYPE a program belongs to: 'food' / 'housing' / ...

    Defaults to FOOD for an unknown or blank program name. That default is
    deliberate and load-bearing: every internal-service case predating the housing
    work is food, so an unrecognised program must keep behaving exactly as it does
    today rather than silently dropping out of the food pipeline.
    """
    key = (program_name or "").strip().casefold()
    if not key:
        return FOOD_DOMAIN
    return _program_domain_map().get(key) or FOOD_DOMAIN


def case_service_domain(case):
    """The service TYPE of a case, derived from its program name."""
    return program_service_domain(getattr(case, "program_name", ""))


def is_food_case(case):
    """True when this case belongs to the FOOD service type.

    Every existing governing-case resolver filters on this, so a housing case can
    never become the case that drives meals/boxes -- verification, kitchen
    assignment, delivery calendars and Purchase Orders. Without the filter a
    freshly APPROVED housing assessment would outrank an older approved meals case
    under `governing_case_key` (authorization favour, then open, then recency) and
    take over the member's food service.
    """
    return case_service_domain(case) == FOOD_DOMAIN


def non_food_program_names():
    """Program names whose type is NOT food.

    For SQL-side exclusions, where the derived type cannot be expressed as a
    filter. Small by nature (18 housing programs today).
    """
    from api.models import ActiveProgram

    return [
        n for n in ActiveProgram.objects
        .exclude(case_type=ActiveProgram.CaseType.FOOD)
        .values_list("program_name", flat=True) if n
    ]


def non_food_program_q():
    """A ``Q`` matching any non-food program by name, case-insensitively, or None
    when every program is food.

    ``__in`` would be case-sensitive, and a near-miss here fails in the DANGEROUS
    direction -- a housing case slipping through as food is exactly what these
    guards exist to prevent -- so each name is matched with ``iexact``.
    """
    from django.db.models import Q

    names = non_food_program_names()
    if not names:
        return None
    q = Q()
    for name in names:
        q |= Q(program_name__iexact=name)
    return q


# ── Service TYPE CODE of a case (which service within its type) ───────────────
# The type says food vs housing; this says WHICH service -- and for housing that
# distinction decides whether a case can govern at all. Environmental Exposure
# Assessment governs; Home Expense Assistance/Repairs is a work order and never
# governs.


@lru_cache(maxsize=1)
def _program_service_type_map():
    """``{program_name.casefold(): ActiveProgram.service_type}``."""
    from api.models import ActiveProgram

    return {
        (name or "").strip().casefold(): (code or "")
        for name, code in ActiveProgram.objects.values_list(
            "program_name", "service_type",
        )
        if name
    }


@lru_cache(maxsize=1)
def _service_type_label_map():
    """``{label.casefold(): code}`` for ActiveProgram.ServiceType.

    Unite Us sends the service as a LABEL on the case ("Environmental Exposure
    Assessment"), so a case whose program is not in our table can still be
    classified from what the source told us.
    """
    from api.models import ActiveProgram

    return {
        label.strip().casefold(): code
        for code, label in ActiveProgram.ServiceType.choices
    }


def program_service_type_code(program_name):
    """The ServiceType CODE a program delivers, or "" when unknown."""
    key = (program_name or "").strip().casefold()
    if not key:
        return ""
    return _program_service_type_map().get(key) or ""


def case_service_type_code(case):
    """The ServiceType code for a case.

    Our curated program mapping FIRST -- it is the authoritative classification
    and an agent can correct it in Settings > Programs -- then the case's own
    ``service_type`` string as sent by Unite Us, matched against the choice
    labels. Blank when neither resolves.
    """
    code = program_service_type_code(getattr(case, "program_name", ""))
    if code:
        return code
    raw = (getattr(case, "service_type", "") or "").strip().casefold()
    if not raw:
        return ""
    return _service_type_label_map().get(raw) or ""

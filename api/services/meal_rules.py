"""Meal Rules: convert a member's verification dietary data (menu type + food
allergies) into what we send the kitchen, applied at kitchen-assignment time.

Inputs (stored on :class:`~api.models.MemberDietaryProfile` from verification):
  * ``menu_type``       — the admin-managed catalog MenuType NAME the client chose.
  * ``food_allergies``  — multi-select FoodAllergy codes.

Outputs (written back onto the profile):
  * ``kitchen_meal_type``  — what the kitchen prepares (a catalog MenuType name
    or "Allergen Free"); empty when out of orbit.
  * ``kitchen_food_notes`` — a free-text note (e.g. "Beef Free"); empty otherwise.
  * ``status``             — ACTIVE, or OUT_OF_ORBIT when the member can't be
    safely fulfilled (excluded from delivery schedules + Purchase Orders).

The ruleset (confirmed with product), evaluated top-down, first match wins::

    allergies = food_allergies − {none}
    1. "Other" in allergies                         -> OUT OF ORBIT
    2. allergies == {}                              -> ("Standard", "")
    3. allergies == {Shellfish}                     -> ("Standard", "Shellfish Free")
    4. allergies == {Pork}                          -> ("Standard", "Pork Free")
    5. allergies == {Red Meat}                      -> ("Standard", "Beef Free")
    6. allergies == {Fish}  & menu == Standard      -> ("Fish Free", "")
    7. allergies == {Milk}  & menu == Standard      -> ("Dairy Free", "")
    8. anything else (single * w/o a dedicated row, or any combination):
          menu in {Standard, Dairy Free, ...lenient} -> ("Allergen Free", "")
          menu in {Vegetarian, Kosher, Halal}        -> OUT OF ORBIT

where ``* = {Fish, Shellfish, Gluten/Wheat, Tree Nuts, Peanuts, Soy, Sesame,
Milk, Eggs}``. Notes never stack: any multi-allergy combination falls through to
rule 8.
"""
from collections import namedtuple

from api.models import KITCHEN_MEAL_ALLERGEN_FREE, MemberStatus

# Menu-type families (normalized names). "Strict" menus send the member out of
# orbit on any allergy not handled by the simple shared rules; everything else
# (Standard, Dairy Free, Fish Free, unknown) falls back to Allergen Free.
_STRICT_MENUS = {"vegetarian", "kosher", "halal"}

# Single, omittable allergens -> the food note added to a Standard meal.
_NOTE_ALLERGY = {
    "shellfish": "Shellfish Free",
    "pork": "Pork Free",
    "red_meat": "Beef Free",
}

# Standard-menu only: a single allergen that switches the whole meal type.
_STANDARD_MEAL_SWITCH = {
    "fish": "Fish Free",
    "milk": "Dairy Free",
}

MealRule = namedtuple("MealRule", ["out_of_orbit", "kitchen_meal_type", "kitchen_food_notes"])


def _norm(value):
    return (value or "").strip().lower()


def _real_allergies(food_allergies):
    """Normalized allergy codes with the no-op 'none' dropped."""
    return {c for c in (_norm(a) for a in (food_allergies or [])) if c and c != "none"}


def resolve_kitchen_meal(menu_type, food_allergies):
    """Pure resolver implementing the meal-rules table. Returns a ``MealRule``."""
    menu = _norm(menu_type)
    allergies = _real_allergies(food_allergies)

    # 1. The catch-all "Other" allergy can never be safely fulfilled.
    if "other" in allergies:
        return MealRule(True, "", "")

    # 2. No allergies -> a plain Standard meal.
    if not allergies:
        return MealRule(False, "Standard", "")

    # 3-5. A single omittable allergen -> Standard meal + a "X Free" note.
    if len(allergies) == 1:
        (only,) = tuple(allergies)
        note = _NOTE_ALLERGY.get(only)
        if note is not None:
            return MealRule(False, "Standard", note)
        # 6-7. Standard menu only: Fish/Milk switch the whole meal type.
        if menu == "standard":
            switched = _STANDARD_MEAL_SWITCH.get(only)
            if switched is not None:
                return MealRule(False, switched, "")

    # 8. Catch-all: any single * allergen without a dedicated row, or any
    # combination of allergens.
    if menu in _STRICT_MENUS:
        return MealRule(True, "", "")
    return MealRule(False, KITCHEN_MEAL_ALLERGEN_FREE, "")


def apply_to_member(profile, *, save=True):
    """Apply the meal rule to a :class:`MemberDietaryProfile`, writing
    ``status`` / ``kitchen_meal_type`` / ``kitchen_food_notes``.

    Returns ``(result, became_out_of_orbit)`` where ``became_out_of_orbit`` is
    True only on an ACTIVE -> OUT_OF_ORBIT transition (so the caller can emit a
    single timeline event)."""
    result = resolve_kitchen_meal(profile.menu_type, profile.food_allergies)
    was_out = profile.status == MemberStatus.OUT_OF_ORBIT
    if result.out_of_orbit:
        profile.status = MemberStatus.OUT_OF_ORBIT
        profile.kitchen_meal_type = ""
        profile.kitchen_food_notes = ""
    else:
        profile.status = MemberStatus.ACTIVE
        profile.kitchen_meal_type = result.kitchen_meal_type
        profile.kitchen_food_notes = result.kitchen_food_notes
    if save:
        profile.save(update_fields=[
            "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
        ])
    return result, (result.out_of_orbit and not was_out)

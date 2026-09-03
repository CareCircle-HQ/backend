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

# Standardized Out-of-Orbit reason for the Service Fulfillment Eligibility Check
# (menu type + allergies can't be safely fulfilled by the/any kitchen).
MENU_ALLERGY_REASON = "Menu & Allergy Requirements Not Serviceable"

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

# Per-allergen "X Free" note. Used when a capable kitchen serves a member whose
# menu/allergy combination the kitchen-agnostic fallback (rule 8) can't express
# as a single note -- e.g. a Kosher member with Pork + Shellfish allergies gets
# "Pork Free, Shellfish Free" (matching the Williamsburg kitchen seed).
_ALLERGEN_FREE_NOTE = {
    "shellfish": "Shellfish Free",
    "pork": "Pork Free",
    "red_meat": "Beef Free",
    "fish": "Fish Free",
    "milk": "Dairy Free",
    "soy": "Soy Free",
    "wheat": "Gluten Free",
    "sesame": "Sesame Free",
    "eggs": "Egg Free",
    "tree_nuts": "Tree Nut Free",
    "peanuts": "Peanut Free",
}

MealRule = namedtuple("MealRule", ["out_of_orbit", "kitchen_meal_type", "kitchen_food_notes"])


def _norm(value):
    return (value or "").strip().lower()


def _real_allergies(food_allergies):
    """Normalized allergy codes with the no-op 'none' dropped."""
    return {c for c in (_norm(a) for a in (food_allergies or [])) if c and c != "none"}


def _allergen_free_notes(food_allergies):
    """Comma-joined "X Free" notes for each real allergen (excluding the catch-all
    'other'), sorted for a stable order (e.g. "Pork Free, Shellfish Free")."""
    notes = {
        _ALLERGEN_FREE_NOTE.get(code, f"{code.replace('_', ' ').title()} Free")
        for code in _real_allergies(food_allergies)
        if code != "other"
    }
    return ", ".join(sorted(notes))


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
    """Apply the GLOBAL meal rule to a :class:`MemberDietaryProfile`, writing
    ``status`` / ``kitchen_meal_type`` / ``kitchen_food_notes``.

    Kitchen-agnostic (does not consider a specific kitchen's capabilities). For
    the kitchen-aware version used on the Household tab and at kitchen
    assignment, see :func:`reconcile_member_kitchen_output`.

    Returns ``(result, became_out_of_orbit)`` where ``became_out_of_orbit`` is
    True only on an ACTIVE -> OUT_OF_ORBIT transition (so the caller can emit a
    single timeline event)."""
    # Local import avoids an import cycle between services modules.
    from api.services.service_area import profile_out_of_range_zip

    result = resolve_kitchen_meal(profile.menu_type, profile.food_allergies)
    # Preserve a member the automatic rule must never reactivate: a manual PAUSE,
    # a terminal INACTIVE, or an eligibility-driven pause (import off-ramp). Only
    # the explicit eligibility-recovery / unpause flow may bring these back, so
    # the automatic meal rule leaves their status untouched.
    if profile.eligibility_paused or profile.status in (
        MemberStatus.PAUSED, MemberStatus.INACTIVE,
    ):
        return result, False
    was_out = profile.status in (MemberStatus.OUT_OF_ORBIT, MemberStatus.OUT_OF_RANGE)
    # Delivery Coverage takes priority: an out-of-area delivery/primary ZIP forces
    # the member out of service even when the meal rule alone could fulfill them.
    # A ZIP outside coverage is Out of Range (a distinct status); a dietary/kitchen
    # fulfillment failure is Out of Orbit.
    zip_excluded = bool(profile_out_of_range_zip(profile))
    out = zip_excluded or result.out_of_orbit
    if out:
        profile.status = (
            MemberStatus.OUT_OF_RANGE if zip_excluded else MemberStatus.OUT_OF_ORBIT
        )
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
    return result, (out and not was_out)


def reconcile_member_kitchen_output(
    profile, kitchen=None, *, offered=None, save=True, allow_resume=False,
):
    """Reconcile a member's kitchen output against BOTH the global meal rules
    and the ASSIGNED kitchen's capabilities, writing ``status`` /
    ``kitchen_meal_type`` / ``kitchen_food_notes``.

    A member is Out of Orbit when any of these hold:
      * their delivery ZIP is outside the coverage area (excluded-ZIP list);
      * no menu type is assigned yet (nothing configured for them);
      * the global meal rule can't safely fulfill the menu + food allergies;
      * a ``kitchen`` is assigned and it doesn't offer the menu type / can't
        handle the member's allergies (per ``member_coverage_for_kitchen``).

    When no kitchen is assigned the kitchen-capability check is skipped (the
    household hasn't reached kitchen assignment yet).

    The ONLY status this may change on its own is OUT_OF_ORBIT: an out-of-orbit
    member is returned to ACTIVE when the (new) kitchen can meet their dietary
    needs, and an ACTIVE member is sent OUT_OF_ORBIT when it can't. Every OTHER
    status is respected and left untouched unless ``allow_resume=True``:
      * PAUSED    -- a deliberate manual pause (e.g. the non-primary members of
                     an individual-scope case) must never be silently resumed;
      * INACTIVE  -- a terminal off-ramp (service ended) is never revived;
      * OUT_OF_RANGE -- a delivery/primary ZIP outside coverage is a geographic
                     block a kitchen change can't fix, so kitchen assignment must
                     not reactivate it.
    So kitchen assignment, imports and dietary re-checks (which call this for
    EVERY household member) can only ever toggle ACTIVE <-> OUT_OF_ORBIT. Only an
    explicit unpause / restore-range flow passes ``allow_resume=True`` to move a
    member off PAUSED / INACTIVE / OUT_OF_RANGE (the ZIP re-check still applies).

    Returns ``(out_of_orbit, became_out, reason)`` where ``became_out`` is True
    only on an ACTIVE -> OUT_OF_ORBIT transition (so the caller emits a single
    timeline event) and ``reason`` explains an out-of-orbit outcome. A preserved
    PAUSED / INACTIVE / OUT_OF_RANGE member returns ``(False, False, "")`` (no
    mutation).
    """
    # Respect every non-automatic status: only an explicit resume/restore flow
    # (allow_resume=True) may bring a PAUSED, terminal INACTIVE or OUT_OF_RANGE
    # member back. Guarded here (the shared entry point) so every caller is
    # protected, not just kitchen assignment -- the automatic meal rule may only
    # move members between ACTIVE and OUT_OF_ORBIT.
    if not allow_resume and profile.status in (
        MemberStatus.PAUSED, MemberStatus.INACTIVE, MemberStatus.OUT_OF_RANGE,
    ):
        return False, False, ""
    # An eligibility-driven pause (import off-ramp) is CareCircle-unfixable: NOT
    # even an explicit resume (allow_resume=True) may reactivate it here. Only the
    # eligibility-recovery flow (which clears eligibility_paused) returns them.
    if profile.eligibility_paused:
        return False, False, ""

    # Local imports avoid any import cycle between services modules.
    from api.services.kitchens import (
        member_coverage_for_kitchen,
        serving_kitchens_for_member,
    )
    from api.services.service_area import SERVICE_AREA_REASON, profile_out_of_range_zip

    was_out = profile.status in (MemberStatus.OUT_OF_ORBIT, MemberStatus.OUT_OF_RANGE)
    out, reason, meal_type, notes = False, "", "", ""
    # A ZIP outside coverage is Out of Range (a distinct status); every other
    # exclusion below is a dietary/kitchen fulfillment block -> Out of Orbit.
    out_of_range = False

    # Delivery Coverage Eligibility Check (highest priority): a member whose
    # delivery/primary ZIP is outside the coverage area is Out of Range regardless
    # of their menu/allergy fulfillment, and stays that way across re-runs.
    if profile_out_of_range_zip(profile):
        out, reason, out_of_range = True, SERVICE_AREA_REASON, True
    elif not (profile.menu_type or "").strip():
        out, reason = True, "No menu type assigned yet."
    else:
        result = resolve_kitchen_meal(profile.menu_type, profile.food_allergies)
        allergies = _real_allergies(profile.food_allergies)
        if "other" in allergies:
            # Rule 1 is absolute: an unspecified "Other" allergy can never be
            # guaranteed safe, no matter what the kitchen offers.
            out, reason = True, MENU_ALLERGY_REASON
        elif kitchen is not None:
            # With a kitchen assigned, ITS capabilities are authoritative. If the
            # kitchen offers the member's menu type and can handle every allergen,
            # the member is serviceable -- even for a "strict" menu
            # (Kosher/Halal/Vegetarian) that the kitchen-agnostic fallback
            # (rule 8) would otherwise send Out of Orbit. In that case we serve
            # the member's own menu type with per-allergen "X Free" notes (e.g.
            # Kosher + Pork/Shellfish -> "Pork Free, Shellfish Free").
            covered, _cov_reason, _price = member_coverage_for_kitchen(
                profile, kitchen, offered=offered,
            )
            if not covered:
                out, reason = True, MENU_ALLERGY_REASON
            elif result.out_of_orbit:
                meal_type = (profile.menu_type or "").strip()
                notes = _allergen_free_notes(profile.food_allergies)
            else:
                meal_type, notes = result.kitchen_meal_type, result.kitchen_food_notes
        elif not result.out_of_orbit:
            # No kitchen assigned yet, and the kitchen-agnostic rule already
            # produces a fulfillable output.
            meal_type, notes = result.kitchen_meal_type, result.kitchen_food_notes
        elif serving_kitchens_for_member(profile):
            # No kitchen assigned yet AND the kitchen-agnostic fallback (rule 8)
            # can't express this "strict" menu + allergy combo -- but at least
            # one ACTIVE kitchen CAN serve the member (the specific kitchen is
            # chosen later at assignment). Keep them serviceable, serving their
            # own menu type with per-allergen "X Free" notes.
            meal_type = (profile.menu_type or "").strip()
            notes = _allergen_free_notes(profile.food_allergies)
        else:
            # No kitchen can serve this menu + allergy combination.
            out, reason = True, MENU_ALLERGY_REASON

    if out:
        profile.status = (
            MemberStatus.OUT_OF_RANGE if out_of_range else MemberStatus.OUT_OF_ORBIT
        )
        profile.kitchen_meal_type = ""
        profile.kitchen_food_notes = ""
    else:
        profile.status = MemberStatus.ACTIVE
        profile.kitchen_meal_type = meal_type
        profile.kitchen_food_notes = notes
    if save:
        profile.save(update_fields=[
            "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
        ])
    return out, (out and not was_out), reason

"""Audit / backfill MemberDietaryProfile dietary values that are stored as enum
CODES (e.g. ``tree_nuts``, ``other``, ``none``) instead of the app-canonical
DietaryTag LABELS (``Tree Nuts``, ``Others``, ``None``).

The verification wizard and Household edit store the DietaryTag NAME (label);
the Excel import (``import_meal_verifications``) stored enum codes, so imported
members have code-form values that display as raw codes in the Household tab.

Dry-run by default -- prints every member that needs fixing and the exact
proposed change. Pass ``--apply`` to write the label form back.

    python manage.py fix_dietary_labels            # audit only
    python manage.py fix_dietary_labels --apply     # backfill
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import MemberDietaryProfile

# Canonical enum CODE -> app DietaryTag LABEL (must match the DietaryTag names
# in the DB exactly).
_ALLERGY_LABEL = {
    "none": "None",
    "soy": "Soy",
    "wheat": "Wheat",
    "sesame": "Sesame",
    "red_meat": "Red Meat",
    "pork": "Pork",
    "milk": "Milk",
    "eggs": "Eggs",
    "fish": "Fish",
    "shellfish": "Shellfish",
    "tree_nuts": "Tree Nuts",
    "peanuts": "Peanuts",
    "other": "Others",
}
_RESTRICTION_LABEL = {
    "none": "No restrictions",
    "diabetes": "Diabetes",
    "postpartum": "Postpartum",
    "cardio_metabolic": "Cardio metabolic",
}

# Map an arbitrary stored value (code OR label) to its canonical code so we can
# compare against the target label. Labels differ from codes only by case/space
# ("Red Meat" -> "red_meat"); a few need an explicit alias.
_ALIAS = {
    "others": "other",
    "no_restrictions": "none",
    "no_restriction": "none",
}


def _canon(value):
    s = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _ALIAS.get(s, s)


def _fix_list(values, label_map):
    """Return (fixed_list, changed) where each known value is replaced by its
    canonical label. Unknown values are left untouched."""
    out, changed = [], False
    for v in values or []:
        code = _canon(v)
        target = label_map.get(code)
        if target is None:
            out.append(v)  # unknown/custom value -- leave as-is
            continue
        if v != target:
            changed = True
        out.append(target)
    # de-dupe while preserving order (a code + its label could collapse)
    seen, deduped = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    if deduped != (values or []):
        changed = True
    return deduped, changed


class Command(BaseCommand):
    help = "Audit (dry-run) or backfill code-form dietary values to label form."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the label form back (default: dry-run audit only).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        to_fix = []
        for p in MemberDietaryProfile.objects.select_related("client", "enrollment").all():
            new_allerg, a_changed = _fix_list(p.food_allergies, _ALLERGY_LABEL)
            new_restr, r_changed = _fix_list(p.dietary_restrictions, _RESTRICTION_LABEL)
            if a_changed or r_changed:
                to_fix.append((p, new_allerg, new_restr, a_changed, r_changed))

        if not to_fix:
            self.stdout.write(self.style.SUCCESS("No profiles need fixing."))
            return

        self.stdout.write(f"{len(to_fix)} member profile(s) need fixing:\n")
        for p, new_allerg, new_restr, a_changed, r_changed in to_fix:
            who = p.member_name or (p.client_id and f"client {p.client_id}") or f"profile {p.pk}"
            self.stdout.write(
                f"- {who}  (profile {p.pk}, enrollment {p.enrollment_id}, client {p.client_id})"
            )
            if a_changed:
                self.stdout.write(f"    allergies:    {p.food_allergies!r}  ->  {new_allerg!r}")
            if r_changed:
                self.stdout.write(f"    restrictions: {p.dietary_restrictions!r}  ->  {new_restr!r}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only. Re-run with --apply to write these changes."
            ))
            return

        with transaction.atomic():
            for p, new_allerg, new_restr, a_changed, r_changed in to_fix:
                if a_changed:
                    p.food_allergies = new_allerg
                if r_changed:
                    p.dietary_restrictions = new_restr
                p.save(update_fields=["food_allergies", "dietary_restrictions", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"\nUpdated {len(to_fix)} profile(s)."))

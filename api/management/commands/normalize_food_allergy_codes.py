"""Normalize ``MemberDietaryProfile.food_allergies`` to canonical FoodAllergy
CODES.

The catch-all + per-allergen values have historically been stored two ways -- as
enum CODES (from the extension / CSV import, e.g. ``"red_meat"``, ``"other"``)
and as tag LABELS (from the verification wizard / CRM editor, e.g. ``"Red Meat"``,
``"Others"``). This left the editor showing the wrong selection state and the
meal-rule / PO logic (which key on codes) disagreeing with the UI.

This command rewrites each stored value to its FoodAllergy code:
``"Milk" -> "milk"``, ``"Red Meat" -> "red_meat"``, etc. Values already in code
form are left as-is; unrecognized values are left untouched and reported.

The catch-all ``"Others" -> "other"`` conversion is GATED behind
``--include-other`` because it is functionally significant: once a member's
allergy reads the code ``other``, meal-rule Rule 1 sends them Out of Orbit on the
next reconcile. Leave it off (default) to normalize only the concrete allergens
until that policy decision is made.

DRY-RUN by default (reports what WOULD change); pass ``--apply`` to commit.
"""

from django.core.management.base import BaseCommand

from api.models import FoodAllergy, MemberDietaryProfile

_OTHER_KEYS = {"other", "others"}


class Command(BaseCommand):
    help = (
        "Rewrite MemberDietaryProfile.food_allergies label values to FoodAllergy "
        "codes (dry-run; --apply). Catch-all 'Others'->'other' needs --include-other."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Commit the changes (default is a dry-run).")
        parser.add_argument("--include-other", action="store_true",
                            help="Also convert the catch-all 'Others' -> 'other' "
                                 "(makes those members Out-of-Orbit-eligible).")
        parser.add_argument("--client", default="",
                            help="Only normalize this client_id (default: all).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        include_other = opts["include_other"]
        only = (opts.get("client") or "").strip()

        codes = {c for c, _ in FoodAllergy.choices}
        label_to_code = {label.strip().lower(): code for code, label in FoodAllergy.choices}

        qs = MemberDietaryProfile.objects.exclude(food_allergies=[])
        if only:
            qs = qs.filter(client__client_id=only)

        changed = other_converted = unknown_left = 0
        conversions = {}  # "from -> to" -> count
        unknowns = {}     # value -> count

        for p in qs.iterator(chunk_size=1000):
            new, seen, dirty = [], set(), False
            for v in (p.food_allergies or []):
                raw = (v or "").strip()
                key = raw.lower()
                if key in _OTHER_KEYS:
                    if not include_other:
                        code = raw  # leave the catch-all untouched for now
                    else:
                        code = "other"
                        if raw != "other":
                            other_converted += 1
                elif raw in codes:
                    code = raw
                elif key in label_to_code:
                    code = label_to_code[key]
                else:
                    code = raw
                    unknown_left += 1
                    unknowns[raw] = unknowns.get(raw, 0) + 1
                if code != raw:
                    conversions[f"{raw} -> {code}"] = conversions.get(f"{raw} -> {code}", 0) + 1
                    dirty = True
                if code not in seen:  # de-dupe, preserve order
                    seen.add(code)
                    new.append(code)
                else:
                    dirty = True
            if dirty and new != list(p.food_allergies or []):
                changed += 1
                if apply:
                    p.food_allergies = new
                    p.save(update_fields=["food_allergies"])

        self.stdout.write("Conversions:")
        for k, n in sorted(conversions.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {k}: {n}")
        if unknowns:
            self.stdout.write("Unrecognized values left untouched:")
            for v, n in sorted(unknowns.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {v!r}: {n}")
        if not include_other:
            self.stdout.write(self.style.WARNING(
                "\nCatch-all 'Others'/'other' left untouched (use --include-other "
                "to convert; note it triggers Out of Orbit)."
            ))

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}: {changed} profile(s) normalized"
            + (f", {other_converted} 'Others'->'other'" if include_other else "")
            + f", {unknown_left} unrecognized value(s) left as-is."
        ))

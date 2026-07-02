"""Idempotently ensure the meal MenuType + Kitchen catalog needed by the
meal-verification import (``import_meal_verifications``).

Ensures:
  * the catalog ``MenuType`` rows exist, renaming the legacy ``Standart`` typo
    to ``Standard`` (and fixing any stored member references to it);
  * the kitchens ENG / AST / Hicksville / Williamsburg exist with the right
    supported products and offered menu types (prices + allergy restrictions).

Safe to re-run: existing rows are updated in place, nothing is deleted. Kitchen
contact emails are intentionally left untouched (configure real ones in admin).

Usage:
    python manage.py seed_meal_catalog            # DRY RUN (rolls back)
    python manage.py seed_meal_catalog --apply     # commit
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    DietaryTag,
    Kitchen,
    KitchenMenuType,
    KitchenStatus,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    MenuType,
)

_MENU_TYPES = ["Standard", "Dairy Free", "Fish Free", "Halal", "Kosher", "Vegetarian"]

# kitchen -> (supported_products, max_orders_per_day, {menu_name: (price, [restriction tag names])})
_CATALOG = {
    "AST": (["meal"], 200, {
        "Standard": (None, ["Others"]),
    }),
    "ENG": (["meal"], 1000, {
        "Standard": (Decimal("7.99"), []),
        "Dairy Free": (Decimal("6.99"), ["Others"]),
        "Fish Free": (None, []),
        "Vegetarian": (None, []),
        "Halal": (None, []),
        "Kosher": (None, []),
    }),
    "Hicksville": (["box"], 50000, {
        "Standard": (None, []),
        "Fish Free": (None, []),
        "Vegetarian": (None, []),
        "Dairy Free": (None, []),
    }),
    "Williamsburg": (["meal"], 20000, {
        "Kosher": (None, ["Others"]),
    }),
}


class Command(BaseCommand):
    help = (
        "Ensure the MenuType + Kitchen catalog for the meal-verification import. "
        "Dry-run unless --apply is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        apply = options["apply"]
        log = self.stdout.write
        head = self.style.MIGRATE_HEADING

        with transaction.atomic():
            log(head("\n=== Seed meal catalog ==="))

            # 1. Fix the Standart -> Standard typo (rename if present).
            legacy = MenuType.objects.filter(name="Standart").first()
            if legacy and not MenuType.objects.filter(name="Standard").exists():
                legacy.name = "Standard"
                legacy.save(update_fields=["name"])
                log("  renamed MenuType 'Standart' -> 'Standard'")
            n1 = MemberDietaryProfile.objects.filter(menu_type="Standart").update(menu_type="Standard")
            n2 = MemberDeliverySchedule.objects.filter(menu_type="Standart").update(menu_type="Standard")
            if n1 or n2:
                log(f"  fixed stored 'Standart' refs: {n1} profiles, {n2} schedules")

            # 1b. Fix the Hicksvile -> Hicksville kitchen typo (rename if present).
            legacy_k = Kitchen.objects.filter(name="Hicksvile").first()
            if legacy_k and not Kitchen.objects.filter(name="Hicksville").exists():
                legacy_k.name = "Hicksville"
                legacy_k.save(update_fields=["name"])
                log("  renamed Kitchen 'Hicksvile' -> 'Hicksville'")

            # 2. Ensure MenuTypes.
            for name in _MENU_TYPES:
                _, created = MenuType.objects.get_or_create(
                    name=name, defaults={"is_active": True}
                )
                if created:
                    log(f"  + MenuType {name!r}")
            menu_by_name = {m.name: m for m in MenuType.objects.all()}

            # 3. Ensure DietaryTags used as restrictions.
            tag_names = {
                t for _, _, menus in _CATALOG.values()
                for _, tags in menus.values() for t in tags
            }
            tag_by_name = {}
            for tname in tag_names:
                tag, created = DietaryTag.objects.get_or_create(name=tname)
                tag_by_name[tname] = tag
                if created:
                    log(f"  + DietaryTag {tname!r}")

            # 4. Ensure kitchens + their offered menu types.
            for kname, (products, max_per_day, menus) in _CATALOG.items():
                kitchen, created = Kitchen.objects.get_or_create(name=kname)
                kitchen.supported_products = products
                kitchen.max_orders_per_day = max_per_day
                kitchen.status = KitchenStatus.ACTIVE
                kitchen.save(update_fields=["supported_products", "max_orders_per_day", "status"])
                log(f"  {'+ created' if created else '~ updated'} Kitchen {kname!r} products={products}")
                for menu_name, (price, restr_names) in menus.items():
                    menu = menu_by_name[menu_name]
                    kmt, _ = KitchenMenuType.objects.get_or_create(
                        kitchen=kitchen, menu_type=menu
                    )
                    if kmt.menu_type_price != price:
                        kmt.menu_type_price = price
                        kmt.save(update_fields=["menu_type_price"])
                    kmt.restrictions.set([tag_by_name[t] for t in restr_names])

            if not apply:
                transaction.set_rollback(True)
                log(self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit."))
            else:
                log(self.style.SUCCESS("\nAPPLIED (committed)."))

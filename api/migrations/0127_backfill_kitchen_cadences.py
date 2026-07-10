"""Defensive backfill: link each kitchen that has NO cadences yet to the
default cadences matching the products it makes, so enforcing Kitchen.cadences
in kitchen assignment never blocks an already-configured environment.

Only kitchens with zero linked cadences are touched -- explicit per-kitchen
setups (e.g. a box kitchen deliberately on a single-day cadence) are left as-is.
Mapping mirrors the seeded ProductType cadences:
  - meal -> mon_thu, tue_fri
  - box  -> once_a_week
"""
from django.db import migrations

# KitchenProductType code -> default Cadence codes to link.
_PRODUCT_DEFAULT_CADENCES = {
    "meal": ["mon_thu", "tue_fri"],
    "box": ["once_a_week"],
}


def backfill(apps, schema_editor):
    Kitchen = apps.get_model("api", "Kitchen")
    Cadence = apps.get_model("api", "Cadence")
    by_code = {c.code: c for c in Cadence.objects.all()}

    for kitchen in Kitchen.objects.all():
        if kitchen.cadences.exists():
            continue  # keep explicit setups untouched
        codes = set()
        for product in (kitchen.supported_products or []):
            codes.update(_PRODUCT_DEFAULT_CADENCES.get(product, []))
        cadences = [by_code[c] for c in codes if c in by_code]
        if cadences:
            kitchen.cadences.set(cadences)


def noop(apps, schema_editor):
    # Irreversible: we can't tell backfilled links from manual ones, so leave
    # the data in place on reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0126_client_is_new_historicalclient_is_new"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]

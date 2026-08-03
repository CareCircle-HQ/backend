"""Seed kitchen abbreviations (used in PO numbers) for the known facilities.

Idempotent + reversible: matches kitchens by (case-insensitive) exact name and
sets the abbreviation. New kitchens / renamed facilities are set from Settings.
"""
from django.db import migrations

# Facility name -> PO abbreviation. Self-aliases (e.g. "ENG" -> "ENG") make the
# seed robust whether the kitchen is named by its facility name ("Englewood") or
# already by its short code ("ENG").
ABBREVIATIONS = {
    "Englewood": "ENG",
    "ENG": "ENG",
    "Astoria": "AST",
    "AST": "AST",
    "Hicksville": "HCK",
    "Rockland": "RCK",
    "Rockland Meals": "RCKM",
    "Williamsburg": "WILL",
}


def seed(apps, schema_editor):
    Kitchen = apps.get_model("api", "Kitchen")
    for name, abbr in ABBREVIATIONS.items():
        Kitchen.objects.filter(name__iexact=name).update(abbreviation=abbr)


def unseed(apps, schema_editor):
    Kitchen = apps.get_model("api", "Kitchen")
    for name, abbr in ABBREVIATIONS.items():
        Kitchen.objects.filter(name__iexact=name, abbreviation=abbr).update(
            abbreviation=""
        )


class Migration(migrations.Migration):

    dependencies = [("api", "0167_kitchen_abbreviation")]

    operations = [migrations.RunPython(seed, unseed)]

"""Seed the initial delivery-coverage excluded ZIP codes.

These are the ZIPs outside our service area at launch; the list is editable from
Settings afterward. Idempotent: only creates ZIPs that are missing.
"""
from django.db import migrations

_SEED_ZIPS = [
    "11209", "11219", "11220", "11228",
    "11355", "11368", "11373", "11377",
]


def seed(apps, schema_editor):
    ExcludedZipCode = apps.get_model("api", "ExcludedZipCode")
    for z in _SEED_ZIPS:
        ExcludedZipCode.objects.get_or_create(zip=z)


def unseed(apps, schema_editor):
    ExcludedZipCode = apps.get_model("api", "ExcludedZipCode")
    ExcludedZipCode.objects.filter(zip__in=_SEED_ZIPS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0118_excludedzipcode"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

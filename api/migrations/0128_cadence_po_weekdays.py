"""Add a configurable PO/cutoff weekday map to Cadence and seed it for the
existing cadences so behavior is unchanged.

``po_weekdays`` maps a delivery weekday code to the weekday its purchase order
is cut on. Seeding mirrors the legacy hardcoded maps in
``api.services.purchase_orders`` (meals: Mon<-Thu, Thu<-Mon, Tue<-Fri, Fri<-Tue;
anything else defaults to Friday, matching the box "cut the Friday before"
rule). Once-a-week cadences (no fixed weekday) get an empty map and keep falling
back to the legacy default.
"""
from django.db import migrations, models

# Legacy meal delivery weekday -> PO weekday; other days default to Friday.
_MEAL_PO = {"mon": "thu", "thu": "mon", "tue": "fri", "fri": "tue"}


def seed(apps, schema_editor):
    Cadence = apps.get_model("api", "Cadence")
    for c in Cadence.objects.all():
        if c.po_weekdays:
            continue
        po = {d: _MEAL_PO.get(d, "fri") for d in (c.weekdays or [])}
        if po:
            c.po_weekdays = po
            c.save(update_fields=["po_weekdays"])


def unseed(apps, schema_editor):
    Cadence = apps.get_model("api", "Cadence")
    Cadence.objects.update(po_weekdays={})


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0127_backfill_kitchen_cadences"),
    ]

    operations = [
        migrations.AddField(
            model_name="cadence",
            name="po_weekdays",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(seed, unseed),
    ]

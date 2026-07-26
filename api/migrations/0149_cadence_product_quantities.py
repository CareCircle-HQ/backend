from django.db import migrations, models

import api.models


def backfill_from_product_types(apps, schema_editor):
    """Seed each cadence's ``product_quantities`` in the new shape.

    Meals store a weekly target (``per_week``, derived from the legacy per-day
    rate x 7, default 21) plus an empty ``per_delivery`` map that an agent fills
    in once delivery days are chosen. Boxes store a per-DAY rate (``per_day``,
    default 1) so a delivery covering N days carries N boxes. Cadences with no
    matching ProductType keep the model default (21 meals/week, 1 box/day)."""
    Cadence = apps.get_model("api", "Cadence")
    ProductType = apps.get_model("api", "ProductType")

    for cadence in Cadence.objects.all():
        quantities = {
            "meals": {"per_week": 21, "per_delivery": {}},
            "boxes": {"per_day": 1},
        }
        meals = ProductType.objects.filter(
            type="meals", delivery_days_cadence=cadence.code
        ).first()
        if meals is not None:
            quantities["meals"] = {
                "per_week": (meals.meals_per_day or 0) * 7,
                "per_delivery": {},
            }
        cadence.product_quantities = quantities
        cadence.save(update_fields=["product_quantities"])


def noop_reverse(apps, schema_editor):
    """No reverse data step -- dropping the column (reverse of AddField) removes
    the data."""


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0148_rename_programpipeline_to_activeprogram"),
    ]

    operations = [
        migrations.AddField(
            model_name="cadence",
            name="product_quantities",
            field=models.JSONField(
                blank=True,
                default=api.models.default_cadence_product_quantities,
            ),
        ),
        migrations.RunPython(backfill_from_product_types, noop_reverse),
    ]

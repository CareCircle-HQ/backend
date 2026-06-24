from django.db import migrations


# Seed values: Meals are delivered twice a week (e.g. 9 meals per delivery),
# Boxes once a week (1 box). These are starting defaults and can be edited later.
SEED = [
    {"type": "meals", "prod_per_delivery": 9, "delivery_days_cadence": "mon_thu"},
    {"type": "boxes", "prod_per_delivery": 1, "delivery_days_cadence": "once_a_week"},
]


def seed_product_types(apps, schema_editor):
    ProductType = apps.get_model("api", "ProductType")
    for row in SEED:
        ProductType.objects.get_or_create(
            type=row["type"],
            defaults={
                "prod_per_delivery": row["prod_per_delivery"],
                "delivery_days_cadence": row["delivery_days_cadence"],
            },
        )


def unseed_product_types(apps, schema_editor):
    ProductType = apps.get_model("api", "ProductType")
    ProductType.objects.filter(type__in=[r["type"] for r in SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0073_producttype_program_product_type"),
    ]

    operations = [
        migrations.RunPython(seed_product_types, unseed_product_types),
    ]

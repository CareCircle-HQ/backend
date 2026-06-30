from django.db import migrations

# The Kitchen Assignment modal offers one cadence per distinct
# ProductType.delivery_days_cadence of the program's kind
# (see api.services.delivery.cadence_options_for_kind). Meals deliveries run on
# two schedules -- Mon/Thu and Tue/Fri -- so both ProductType rows must exist for
# the agent to choose between them. This seed guarantees both are present.
#
# Idempotent: get_or_create keys on the (type, delivery_days_cadence) unique
# constraint, so it never duplicates and never overwrites an existing row's
# quantities. meals_per_day == prod_per_delivery (3) mirrors the configured rows.
MEALS_CADENCES = [
    # (delivery_days_cadence, prod_per_delivery, meals_per_day)
    ("mon_thu", 3, 3),
    ("tue_fri", 3, 3),
]


def seed_meals_cadences(apps, schema_editor):
    ProductType = apps.get_model("api", "ProductType")
    for cadence, prod_per_delivery, meals_per_day in MEALS_CADENCES:
        ProductType.objects.get_or_create(
            type="meals",
            delivery_days_cadence=cadence,
            defaults={
                "prod_per_delivery": prod_per_delivery,
                "meals_per_day": meals_per_day,
            },
        )


def noop(apps, schema_editor):
    # Reverse is a no-op: we don't delete catalog rows that may now be in use by
    # delivery schedules / purchase orders.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0103_remap_removed_stages"),
    ]

    operations = [
        migrations.RunPython(seed_meals_cadences, noop),
    ]

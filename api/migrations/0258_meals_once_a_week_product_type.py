"""Add the missing MEALS x Once-a-Week ProductType.

Once-a-week was only ever configured for BOXES, because the only meal cadences
were Mon/Thu and Tue/Fri. Williamsburg now delivers meals Wed-Only (once a week),
which left the pairing unconfigured and:

  * raised the "Cadence doesn't match product type" household warning -- correctly:
    ``check_cadence_kind_mismatch`` sees once_a_week set up for boxes only;
  * made ``_resolve_product_type`` fall back to "any meals ProductType" (so plans
    silently carried the Mon/Thu row), or resolve to NOTHING when the program name
    has no meal/box keyword -- leaving meals_per_day=0, which ``plan_built_kind``
    reads as "not meals".

``meals_per_day`` is per DAY, so it is 3 exactly like the other meal cadences; the
weekly total is identical either way (Mon/Thu = 9 + 12, Wed-Only = 1 x 21 = 21).
``prod_per_delivery`` mirrors the other meals rows: for meals the per-delivery
quantity is computed from meals_per_day x coverage days, so this value is nominal
-- but it must not be the ONLY non-zero field, or plan_built_kind would read the
plan as boxes.
"""

from django.db import migrations

MEALS_PER_DAY = 3
PROD_PER_DELIVERY = 3


def add_product_type(apps, schema_editor):
    ProductType = apps.get_model("api", "ProductType")
    obj, created = ProductType.objects.get_or_create(
        type="meals",
        delivery_days_cadence="once_a_week",
        defaults={
            "prod_per_delivery": PROD_PER_DELIVERY,
            "meals_per_day": MEALS_PER_DAY,
        },
    )
    print(f"  meals/once_a_week ProductType {'created' if created else 'already present'}")


def remove_product_type(apps, schema_editor):
    apps.get_model("api", "ProductType").objects.filter(
        type="meals", delivery_days_cadence="once_a_week"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0257_alter_client_source_alter_enrollmentanalytics_source_and_more"),
    ]

    operations = [
        migrations.RunPython(add_product_type, remove_product_type),
    ]

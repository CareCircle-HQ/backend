"""Seed the default CadenceRule rows for Meals and Boxes.

Meals cadence is chosen by the weekday the case becomes active. The cadence name
refers to the PO/cutoff days; deliveries land on the complementary pair:
  - "Tue/Fri" cadence  -> deliveries Mon & Thu, POs Tue & Fri
  - "Mon/Thu" cadence  -> deliveries Tue & Fri, POs Mon & Thu

Wednesday is a custom edge case: it folds into the Thursday branch (cadence
Tue/Fri, first delivery the following Monday, skipping that week's Thursday).

Boxes are always once a week: delivered Wednesday, POs generated Friday.
"""
from django.db import migrations

# (accepted_weekday, cadence, delivery_weekdays, po_weekdays, first_delivery_weekday)
MEALS_RULES = [
    (0, "tue_fri", ["mon", "thu"], ["tue", "fri"], 3),  # Mon -> first Thu
    (1, "mon_thu", ["tue", "fri"], ["mon", "thu"], 4),  # Tue -> first Fri
    (2, "tue_fri", ["mon", "thu"], ["tue", "fri"], 0),  # Wed (edge) -> first Mon
    (3, "tue_fri", ["mon", "thu"], ["tue", "fri"], 0),  # Thu -> first Mon
    (4, "mon_thu", ["tue", "fri"], ["mon", "thu"], 1),  # Fri -> first Tue
]

# Boxes: same rule regardless of the accept day (Mon-Fri).
BOXES_RULES = [
    (wd, "once_a_week", ["wed"], ["fri"], 2) for wd in range(5)
]


def seed(apps, schema_editor):
    CadenceRule = apps.get_model("api", "CadenceRule")
    rows = [("meals", *r) for r in MEALS_RULES] + [("boxes", *r) for r in BOXES_RULES]
    for kind, weekday, cadence, delivery, po, first in rows:
        CadenceRule.objects.update_or_create(
            product_kind=kind,
            accepted_weekday=weekday,
            defaults={
                "cadence": cadence,
                "delivery_weekdays": delivery,
                "po_weekdays": po,
                "first_delivery_weekday": first,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    CadenceRule = apps.get_model("api", "CadenceRule")
    CadenceRule.objects.filter(product_kind__in=["meals", "boxes"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0077_cadencerule_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

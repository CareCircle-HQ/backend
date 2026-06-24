"""Realign meal CadenceRule rows to the agreed schedule + live cadence naming.

Two problems with the original seed (migration 0078) are fixed here:

1. Naming inversion: 0078 treats the cadence NAME as the PO days (so cadence
   "mon_thu" delivers Tue/Fri). The live manual-cadence flow
   (api/services/delivery.py CADENCE_WEEKDAYS) treats the cadence name as the
   DELIVERY days ("mon_thu" delivers Mon/Thu). The live code is authoritative.
2. PO/delivery pairing: per the agreed rule the PO day and its delivery land on
   the SAME cadence pair (Mon PO -> Thu delivery, Thu PO -> next Mon; Tue PO ->
   Fri delivery, Fri PO -> next Tue).

So for every meal rule we set both delivery_weekdays AND po_weekdays to the
cadence-named pair, making CadenceRule consistent with the live code and the PO
schedule. Boxes are unchanged (deliver Wed, PO Fri). first_delivery_weekday is
left as-is (unused by the live flow).
"""
from django.db import migrations

# Cadence name == delivery weekdays == PO weekdays.
MEAL_PAIR = {
    "mon_thu": ["mon", "thu"],
    "tue_fri": ["tue", "fri"],
}

# Original 0078 values (for reverse), keyed by accepted_weekday:
# (cadence, delivery_weekdays, po_weekdays)
ORIGINAL_0078 = {
    0: ("tue_fri", ["mon", "thu"], ["tue", "fri"]),
    1: ("mon_thu", ["tue", "fri"], ["mon", "thu"]),
    2: ("tue_fri", ["mon", "thu"], ["tue", "fri"]),
    3: ("tue_fri", ["mon", "thu"], ["tue", "fri"]),
    4: ("mon_thu", ["tue", "fri"], ["mon", "thu"]),
}


def realign(apps, schema_editor):
    CadenceRule = apps.get_model("api", "CadenceRule")
    for rule in CadenceRule.objects.filter(product_kind="meals"):
        pair = MEAL_PAIR.get(rule.cadence)
        if pair is not None:
            rule.delivery_weekdays = list(pair)
            rule.po_weekdays = list(pair)
            rule.save(update_fields=["delivery_weekdays", "po_weekdays"])


def revert(apps, schema_editor):
    CadenceRule = apps.get_model("api", "CadenceRule")
    for rule in CadenceRule.objects.filter(product_kind="meals"):
        original = ORIGINAL_0078.get(rule.accepted_weekday)
        if original is not None:
            _, delivery, po = original
            rule.delivery_weekdays = list(delivery)
            rule.po_weekdays = list(po)
            rule.save(update_fields=["delivery_weekdays", "po_weekdays"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0083_deliveryorder_default_kitchen_deliveryorder_rerouted_and_more"),
    ]

    operations = [
        migrations.RunPython(realign, revert),
    ]

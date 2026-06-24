# Re-syncs DeliveryOrder.quantity from the (now coverage-corrected)
# OrderSchedule.how_many_meals_or_boxes. The original 0085 backfill ran before
# 0086 fixed meals quantities (3/day -> 9 then 12), so meals delivery orders
# created earlier hold a stale per-delivery quantity. Boxes are unaffected.

from django.db import migrations


def resync_quantity(apps, schema_editor):
    DeliveryOrder = apps.get_model("api", "DeliveryOrder")
    OrderSchedule = apps.get_model("api", "OrderSchedule")
    for do in DeliveryOrder.objects.select_related("purchase_order").all():
        sched = (
            OrderSchedule.objects.filter(
                member__client=do.member_id,
                anticipated_delivery_date=do.expected_delivery_date,
                household=do.group_id,
            )
            .exclude(how_many_meals_or_boxes__isnull=True)
            .first()
        )
        if sched is not None and do.quantity != sched.how_many_meals_or_boxes:
            do.quantity = sched.how_many_meals_or_boxes
            do.save(update_fields=["quantity"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0086_memberdeliveryschedule_meals_per_day_and_more'),
    ]

    operations = [
        migrations.RunPython(resync_quantity, noop),
    ]

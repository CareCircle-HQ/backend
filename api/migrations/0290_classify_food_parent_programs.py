"""Classify the internal Food programmes by parent product, and rename the
service type of the ones we deliver as boxes.

Three changes, in this order:

1. The 24 INTERNAL food programmes on ``food_prescriptions`` move to
   ``produce_prescription`` ("Produce Prescription/Voucher"). Only the internal
   ones: the other 24 are external providers' programmes whose service type is
   what Unite Us sends us, and renaming those would stop them matching.

2. Internal + ``medically_tailored_meals``  -> parent_program = meals   (24)
3. Internal + ``produce_prescription``      -> parent_program = boxes   (24)

CLINICALLY APPROPRIATE MEALS IS DELIBERATELY LEFT BLANK. There are 24 internal CAM
programmes and they are plainly a meal service, so this looks like an omission --
it is not. It was raised and the decision was to leave them unclassified for now.

Idempotent, and safe to re-run: each step filters on what it is about to change.
"""
from django.db import migrations


def classify(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")

    internal_food = ActiveProgram.objects.filter(
        case_type="food", case_category__icontains="internal",
    )

    renamed = internal_food.filter(service_type="food_prescriptions").update(
        service_type="produce_prescription",
    )
    meals = internal_food.filter(service_type="medically_tailored_meals").update(
        parent_program="meals",
    )
    boxes = internal_food.filter(service_type="produce_prescription").update(
        parent_program="boxes",
    )
    print(
        f"\n  food_prescriptions -> produce_prescription: {renamed}"
        f"\n  parent_program=meals (MTM): {meals}"
        f"\n  parent_program=boxes (Produce Prescription): {boxes}"
    )


def unclassify(apps, schema_editor):
    """Reverses cleanly, which matters because this runs on production data.

    The service type goes back to ``food_prescriptions`` for exactly the rows this
    migration moved -- internal food on ``produce_prescription`` -- so a reverse
    cannot catch a row somebody set by hand afterwards for a different reason.
    """
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    internal_food = ActiveProgram.objects.filter(
        case_type="food", case_category__icontains="internal",
    )
    internal_food.filter(service_type="produce_prescription").update(
        service_type="food_prescriptions",
    )
    internal_food.filter(parent_program__in=["meals", "boxes"]).update(
        parent_program="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0289_activeprogram_parent_program"),
    ]

    operations = [
        migrations.RunPython(classify, unclassify),
    ]

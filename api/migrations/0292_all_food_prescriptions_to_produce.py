"""Move the REMAINING programmes off ``food_prescriptions``.

0290 moved the 24 Internal Services rows. This finishes the job for the other 24:

    External Services   12
    Reauthorization     12

WHY ALL OF THEM, including the external ones: "Food Prescriptions (Voucher / Boxes)"
was never what the source system called this service. Every one of the 3,527 cases
in the database carries ``service_type = "Produce Prescription/Voucher"``, and NOT
ONE case has ever carried "Food Prescriptions (Voucher / Boxes)" -- it existed only
as our own label. Leaving any programme on it keeps a value that matches nothing in
Unite Us.

⚠ PARENT_PROGRAM IS NOT TOUCHED, so the 12 Reauthorization rows end up on
``produce_prescription`` with no parent product: findable by service type, but not
under the Boxes filter. That is deliberate -- the decision to leave their parent
blank was made separately -- but it is an odd-looking half state, so it is recorded
here rather than left to be puzzled over.

The external 12 SHOULD stay blank regardless: they are other providers' programmes
and we deliver no product for them.

``food_prescriptions`` remains a valid choice even though no row now uses it.
Historical migrations reference it, and other environments may still hold rows.
"""
from django.db import migrations


def rename(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")

    moved = ActiveProgram.objects.filter(service_type="food_prescriptions").update(
        service_type="produce_prescription",
    )
    print(f"\n  food_prescriptions -> produce_prescription: {moved}")


def unrename(apps, schema_editor):
    """NOT a true inverse, and cannot be.

    By this point ``produce_prescription`` holds rows from three sources: the 24
    internal ones moved by 0290, and the external and reauthorization rows moved
    here. Nothing distinguishes them afterwards, so reversing would have to guess.

    It reverses the rows this migration can still identify -- everything that is NOT
    Internal Services, since 0290 owns those and reverses them itself.
    """
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        service_type="produce_prescription",
    ).exclude(case_category__icontains="internal").update(
        service_type="food_prescriptions",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0291_classify_cam_as_meals"),
    ]

    operations = [
        migrations.RunPython(rename, unrename),
    ]

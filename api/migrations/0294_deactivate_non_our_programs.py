"""Mark as active only the programme categories we actually offer.

    ACTIVE                              programmes   cases   cases <180d
      Internal Services                        101  21,928        21,771
      ELIGIBILITY                                9  54,476        53,919
      Care Management                            5  89,447        89,071

    INACTIVE
      External Services                       162     145           144
      Reauthorization                          33       6             2
      SCREENING                                 8     471           357
      Other                                     6      11            11

The three active categories carry 165,851 of the 166,484 cases in the table, so the
split follows the work rather than just the naming.

⚠ INACTIVE DOES NOT MEAN UNUSED, and two of these are live:

    SCREENING          357 cases in the last six months
    External Services  144 cases in the last six months

The flag says "not one of ours to offer". Nothing may use it to decide whether a
case is real, or those cases become invisible.

⚠ REAUTHORIZATION IS INACTIVE HERE, which is worth a second look one day.
``_CATEGORY_TO_CASE_TYPE`` classifies "reauthorization" AS an Internal Service, and
those 33 programmes extend our own food services -- so by that definition they are
ours. They are inactive because the instruction named three categories and this was
not one of them, and because they carry only 6 cases in total. Recorded rather than
quietly decided.

Matched case-insensitively: the stored values are "Internal Services",
"ELIGIBILITY" and "Care Management", three different conventions in one column, and
an exact match would silently miss a row someone retyped.
"""
from django.db import migrations

OURS = ["internal services", "internal service", "eligibility", "care management"]


def set_active(apps, schema_editor):
    from django.db.models import Q

    ActiveProgram = apps.get_model("api", "ActiveProgram")

    ours = Q()
    for category in OURS:
        ours |= Q(case_category__iexact=category)

    active = ActiveProgram.objects.filter(ours).update(is_active=True)
    inactive = ActiveProgram.objects.exclude(ours).update(is_active=False)
    print(f"\n  active: {active}\n  inactive: {inactive}")


def reset(apps, schema_editor):
    """Back to the field default -- everything active, which is what a table with
    no such flag effectively meant."""
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0293_activeprogram_is_active"),
    ]

    operations = [
        migrations.RunPython(set_active, reset),
    ]

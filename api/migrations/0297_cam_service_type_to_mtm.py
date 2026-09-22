"""Clinically Appropriate Meals programmes take the Medically Tailored Meals type.

    clinically_appropriate_meals -> medically_tailored_meals    24

WHY: the case data has no such service type, and never has. All 3,893 cases sitting
on a Clinically Appropriate Meals programme carry
``service_type = "Medically Tailored Meals"``, and a search for "clinically" across
every text field on Case finds it in the programme name, the description and a
closed note -- but NEVER in service_type:

    cases with service_type 'Clinically Appropriate Meals'   0
    cases on a CAM programme                             3,893
      ... all filed as 'Medically Tailored Meals'

So our programme table was making a distinction Unite Us does not, and anything
comparing a programme's service_type to a case's would have mismatched on all
3,893.

THE DISTINCTION IS NOT LOST. It stays in two places that are better suited to it:

    program_name    still begins "Clinically Appropriate Meals - …"  (24 rows)
    parent_program  meals, for both CAM and MTM                      (48 rows)

After this, medically_tailored_meals covers 48 programmes, which is exactly how the
19,426 cases are already filed:

    12  Clinically Appropriate Meals - …
    12  Reauthorization: Clinically Appropriate Meals - …
    12  Medically Tailored Meals (MTM) - …
    12  Reauthorization: Medically Tailored Meals (MTM) - …

Note the Reauthorization-NAMED rows sit in the "Internal Services" CATEGORY, not
the "Reauthorization" one -- the two do not agree in this table, which is worth
knowing before writing a filter around either.

``clinically_appropriate_meals`` remains a valid choice but is retired from the
dropdown, the same treatment food_prescriptions got in 0292: historical migrations
reference it, and a row still holding it must render rather than go blank.
"""
from django.db import migrations


def to_mtm(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")

    moved = ActiveProgram.objects.filter(
        service_type="clinically_appropriate_meals",
    ).update(service_type="medically_tailored_meals")
    print(f"\n  clinically_appropriate_meals -> medically_tailored_meals: {moved}")


def back_to_cam(apps, schema_editor):
    """Reverses using the PROGRAMME NAME, which is the only thing that still knows.

    After the forward migration the two are indistinguishable by service type, so
    a blanket reverse would drag the real MTM programmes across too.
    """
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    # icontains, NOT istartswith: half of them are named
    # "Reauthorization: Clinically Appropriate Meals - …", so a prefix match would
    # restore 12 of the 24 and silently leave the rest merged.
    ActiveProgram.objects.filter(
        service_type="medically_tailored_meals",
        program_name__icontains="Clinically Appropriate Meals",
    ).update(service_type="clinically_appropriate_meals")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0296_classify_home_remediation"),
    ]

    operations = [
        migrations.RunPython(to_mtm, back_to_cam),
    ]

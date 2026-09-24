"""Clinically Appropriate Meals is a meal service: parent_program = meals.

Completes the classification 0290 deliberately left open. 0290's note said CAM was
being left blank for now; this is the "now" ending.

    clinically_appropriate_meals -> parent_program = meals    (24 on the clone)

⚠ FILTERED ON SERVICE TYPE ALONE, unlike 0290, which also required
``case_category`` to contain "internal". That is deliberate and matches the
instruction ("all programs with service type == Clinically Appropriate Meals"), and
on current data the two are the same rows -- all 24 CAM programmes are Internal
Services / food. The difference only shows up if an EXTERNAL CAM programme ever
appears, which this migration would classify and 0290's rule would not. Worth
knowing rather than discovering.

Only blank rows are written, so a value somebody has set by hand is never
overwritten.
"""
from django.db import migrations


def classify(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")

    updated = ActiveProgram.objects.filter(
        service_type="clinically_appropriate_meals", parent_program="",
    ).update(parent_program="meals")
    print(f"\n  clinically_appropriate_meals -> parent_program=meals: {updated}")


def unclassify(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        service_type="clinically_appropriate_meals", parent_program="meals",
    ).update(parent_program="")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0290_classify_food_parent_programs"),
    ]

    operations = [
        migrations.RunPython(classify, unclassify),
    ]

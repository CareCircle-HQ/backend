"""The Home Remediation programmes get Home Remediation as their parent.

    Internal Services + housing + name starts "Home Remediation"  ->  15

These are the 15 "Home Remediation - <device> - <borough>" programmes: air
conditioner, air filtration device, de-humidifier, heater and humidifier across
Brooklyn, Manhattan and Queens.

ALL THREE CONDITIONS ARE APPLIED, not just the name. "Home Remediation Assistance:
Mold and Pest" and similar appear in the eligibility data as EXTERNAL services, and
a name prefix alone would eventually sweep one of those in. The instruction named
internal + housing, and filtering on all three keeps it that way even as the table
grows.

Only blank rows are written, so a value set by hand is never overwritten.
"""
from django.db import migrations


def classify(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")

    updated = ActiveProgram.objects.filter(
        case_category__icontains="internal",
        case_type="housing",
        program_name__istartswith="Home Remediation",
        parent_program="",
    ).update(parent_program="home_remediation")
    print(f"\n  Home Remediation -> parent_program=home_remediation: {updated}")


def unclassify(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(parent_program="home_remediation").update(
        parent_program="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0295_parent_program_home_remediation"),
    ]

    operations = [
        migrations.RunPython(classify, unclassify),
    ]

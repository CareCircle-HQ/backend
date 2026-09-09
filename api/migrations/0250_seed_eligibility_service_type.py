"""Set ``service_type = social_service_case_management`` on the ELIGIBILITY programs.

Every eligibility case in the CRM carries the service type "Social Service Case
Management" (49,466/49,466), and the 9 program names on those cases map exactly
1:1 to the ``ActiveProgram`` rows classified ``case_category = ELIGIBILITY``:

    Enhanced Care Management - Eligibility Assessment Level 2 Only - {borough}
    Navigation Services - Eligibility Assessment and Navigation to existing
        resources (Level 1) - {FFS,MCO} Members - {borough}

Keyed on ``case_category`` (not the name) so a newly added eligibility program
picks the value up if this is ever re-run. Only fills BLANK rows, so an agent's
manual choice is never overwritten.
"""
from django.db import migrations

CODE = "social_service_case_management"


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        case_category__iexact="ELIGIBILITY", service_type=""
    ).update(service_type=CODE)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(service_type=CODE).update(service_type="")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0249_alter_activeprogram_service_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

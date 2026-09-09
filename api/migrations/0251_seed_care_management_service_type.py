"""Set ``service_type = social_service_case_management`` on the Care Management programs.

The 5 ``ActiveProgram`` rows classified ``case_category = Care Management`` are:

    Enhanced Care Management - Care Management Level 2 Only - {Brooklyn,Manhattan,Queens}
    NY1115 Member Navigation
    Care Management Services            (no cases reference it)

Their cases carry "Social Service Case Management" as the service type (65,503 of
65,504 -- a single NY1115 Member Navigation case carries "Individual & Family
Support" instead), so the program-level service type is unambiguous.

Keyed on ``case_category`` so a newly added care-management program picks the
value up if this is ever re-run, and only fills BLANK rows so an agent's manual
choice is never overwritten. The reverse is scoped to this category so it can't
clear the ELIGIBILITY rows seeded by 0250.
"""
from django.db import migrations

CODE = "social_service_case_management"
CATEGORY = "Care Management"


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        case_category__iexact=CATEGORY, service_type=""
    ).update(service_type=CODE)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        case_category__iexact=CATEGORY, service_type=CODE
    ).update(service_type="")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0250_seed_eligibility_service_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

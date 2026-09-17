"""Correct the Home Accessibility programs' service type.

0268 gave the 11 "Home Accessibility and Safety Modification - <item> -
<borough>" programs ``home_expense_assistance_repairs``, matching the Home
Remediation set. That was wrong -- they deliver a distinct service:

    service_type : 'home_expense_assistance_repairs'
                -> 'environmental_modifications_accessibility'

So housing now has three services, and the distinction is functional rather than
cosmetic -- the Cases tab and the stage bar show the service, and the vendor's
questionnaire is chosen from it:

    Environmental Exposure Assessment            GOVERNS      (3 Dwelling programs)
    Home Expense Assistance/Repairs              work order  (15 Home Remediation)
    Environmental Modifications/Accessibility    work order  (11 Home Accessibility)

Matched by PREFIX here, unlike 0268 which listed 11 names explicitly. The
difference is deliberate: 0268 decided WHICH programs become internal, and the
prefix covers 18 of which only 11 were listed. This migration only corrects the
service type of rows ALREADY converted, so scoping it to
``case_category='Internal Services'`` cannot touch the seven that stayed external.

⚠️ ``WORK_ORDER_SERVICE_TYPES`` in api/services/housing.py had to gain the new code
in the same change. A housing case whose service resolves to neither that set nor
GOVERNING_SERVICE_TYPES is classified as NEITHER an assessment nor a work order --
visible by design rather than silently defaulted, but it means a new housing
service without that entry leaves its cases in limbo.
"""
from django.db import migrations

OLD_CODE = "home_expense_assistance_repairs"
NEW_CODE = "environmental_modifications_accessibility"
PREFIX = "Home Accessibility and Safety Modification - "


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        program_name__istartswith=PREFIX,
        case_category="Internal Services",
        service_type=OLD_CODE,
    ).update(service_type=NEW_CODE)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ActiveProgram.objects.filter(
        program_name__istartswith=PREFIX,
        case_category="Internal Services",
        service_type=NEW_CODE,
    ).update(service_type=OLD_CODE)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0269_home_accessibility_service_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

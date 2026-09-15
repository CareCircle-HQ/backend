"""Set ``service_type = environmental_exposure_assessment`` on the 3 housing programs.

All three Dwelling Assessment / SOW Development programs (Brooklyn, Manhattan,
Queens) deliver the same service, and Unite Us sends it verbatim as the case's
``service_type``:

    Environmental Exposure Assessment

Confirmed against live data before seeding: the one housing case already in the
CRM (program "... - Brooklyn") carries exactly that string, so the program-level
value matches what the importer will see.

Follows 0248/0250/0251: only fills rows whose service_type is still BLANK, so an
agent's manual choice from Settings > Programs is never overwritten. Matched on
exact program names rather than a LIKE pattern, so a future non-internal dwelling
programme cannot be caught by accident.

NOT added to ``api.serializers.INTERNAL_SERVICE_SUBTYPES``, deliberately. That
frozenset means "IS our meal/box service" -- it forces CaseType.INTERNAL_SERVICE
regardless of program, and also drives `purge_out_of_scope_cases`. Housing cases
already classify correctly through the program-name path now that the category is
'Internal Services', so adding a housing subtype there would blur the meal/box
meaning for no gain. Revisit only if housing cases start arriving with a BLANK
program_name, which is the one case the subtype path exists to catch.
"""
from django.db import migrations

CODE = "environmental_exposure_assessment"

PROGRAMS = [
    "Dwelling Assessment & Statement of Work (SOW) Development - "
    "Modifications and Remediation Service - Brooklyn",
    "Dwelling Assessment & Statement of Work (SOW) Development - "
    "Modifications and Remediation Service - Manhattan",
    "Dwelling Assessment & Statement of Work (SOW) Development - "
    "Modifications and Remediation Service - Queens",
]


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    for name in PROGRAMS:
        ActiveProgram.objects.filter(
            program_name__iexact=name, service_type="",
        ).update(service_type=CODE)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    for name in PROGRAMS:
        ActiveProgram.objects.filter(
            program_name__iexact=name, service_type=CODE,
        ).update(service_type="")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0263_housing_service_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

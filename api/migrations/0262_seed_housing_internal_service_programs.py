"""Classify the 3 Dwelling Assessment / SOW programs as Internal Services, type Housing.

We are beginning to PROCESS housing programs, which means bringing housing cases
into the CRM. These three are the housing service WE deliver, so they become
internal services -- of a different TYPE:

    Dwelling Assessment & Statement of Work (SOW) Development -
        Modifications and Remediation Service - {Brooklyn, Manhattan, Queens}

    case_category : 'External Services' -> 'Internal Services'
    case_type     : 'food'             -> 'housing'
    main_category : 'Food'             -> 'Housing'

`case_category` is the AUTHORITATIVE routing label (see
`api.serializers.derive_case_type_from_active_program`): 'Internal Services' maps
to `CaseType.INTERNAL_SERVICE`, so cases on these programs will drive the service
lifecycle rather than being external referrals. `case_type` is the TYPE within
that category, and the rule is one governing internal-service case PER TYPE, so a
member may hold a Food case and a Housing case without them competing.

The existing spelling is 'Internal Services' (plural, 72 rows), not the singular --
`_CATEGORY_TO_CASE_TYPE` accepts both, but matching the existing rows keeps the
Settings > Programs list consistent.

EVERY OTHER housing program stays External. The 7 programs under the active
`ProgramMainCategory` "Housing" (Asthma Remediation, Tenancy Sustaining Services,
Rent/Temporary Housing Rent Payment Assistance, ...) are referrals out, are not in
`ActiveProgram`, and are deliberately untouched here.

SAFE TO RUN NOW: zero cases currently reference these three program names, so this
changes classification for future imports only and no existing case is
reclassified.

Matched on the exact program names rather than a pattern: a `LIKE '%Dwelling%'`
would also catch any future non-internal dwelling program.

Reverse restores the previous values exactly.
"""
from django.db import migrations

PROGRAMS = [
    "Dwelling Assessment & Statement of Work (SOW) Development - "
    "Modifications and Remediation Service - Brooklyn",
    "Dwelling Assessment & Statement of Work (SOW) Development - "
    "Modifications and Remediation Service - Manhattan",
    "Dwelling Assessment & Statement of Work (SOW) Development - "
    "Modifications and Remediation Service - Queens",
]

NEW = {"case_category": "Internal Services", "case_type": "housing",
       "main_category": "Housing"}
OLD = {"case_category": "External Services", "case_type": "food",
       "main_category": "Food"}


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    for name in PROGRAMS:
        ActiveProgram.objects.filter(program_name__iexact=name).update(**NEW)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    for name in PROGRAMS:
        ActiveProgram.objects.filter(
            program_name__iexact=name, case_category=NEW["case_category"],
        ).update(**OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0261_housing_program_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

"""Classify the 15 Home Remediation programs as Internal Services, type Housing.

The second housing service we deliver, after the Dwelling Assessment / SOW
Development programs in 0262/0264. Fifteen rows -- five devices across three
boroughs:

    Home Remediation - {Air Conditioner, Air Filtration Device, De-humidifier,
                        Heater, Humidifier} - {Brooklyn, Manhattan, Queens}

    case_category : 'External Services' -> 'Internal Services'
    case_type     : 'food'              -> 'housing'
    service_type  : ''                  -> 'home_expense_assistance_repairs'
    main_category : 'Food'              -> 'Housing'

As with 0262, `case_category` is the authoritative routing label, so this is also
the switch that lets the importer bring these cases in: `case_in_import_scope`
skips 'External Services', so these programs were being dropped silently.

SAFE TO RUN NOW: zero cases currently reference any of these 15 program names, so
only future imports are affected and nothing existing is reclassified.

Matched on the `Home Remediation - ` PREFIX, unlike 0262/0264 which listed exact
names. There are 15 of them and they share a strict naming scheme, so a prefix is
the honest description of the set -- but it is anchored with `istartswith` and
includes the trailing " - " so it cannot catch, say, a future
"Home Remediation Assistance: Ventilation Improving Systems" (one of the 7
referral-only programs under the active Housing category, which must stay
External).

Only fills service_type on rows where it is still BLANK, following 0248/0250/0251,
so an agent's manual choice is never overwritten. The category/type updates are
scoped to rows still marked External Services for the same reason.
"""
from django.db import migrations

PREFIX = "Home Remediation - "
CODE = "home_expense_assistance_repairs"


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    rows = ActiveProgram.objects.filter(program_name__istartswith=PREFIX)
    rows.filter(case_category="External Services").update(
        case_category="Internal Services", case_type="housing",
        main_category="Housing",
    )
    rows.filter(service_type="").update(service_type=CODE)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    rows = ActiveProgram.objects.filter(program_name__istartswith=PREFIX)
    rows.filter(service_type=CODE).update(service_type="")
    rows.filter(case_category="Internal Services", case_type="housing").update(
        case_category="External Services", case_type="food", main_category="Food",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0265_home_remediation_service_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

"""Classify the 11 listed Home Accessibility programs as Internal Services / Housing.

From "Home Accessibility and Home Remediation Case Names.xlsx" (2026-09-17), which
lists 26 program names:

  * 15 "Home Remediation - <device> - <borough>" -- ALREADY converted by 0266, so
    this migration does not touch them.
  * 11 "Home Accessibility and Safety Modification - <device> - <borough>" -- still
    External Services / food, and reclassified here.

    case_category : 'External Services' -> 'Internal Services'
    case_type     : 'food'              -> 'housing'
    service_type  : ''                  -> 'home_expense_assistance_repairs'
    main_category : 'Food'              -> 'Housing'

NOTHING IS INSERTED. All 26 names already exist in ActiveProgram (seeded by 0158),
so this is a reclassification and duplicates are impossible by construction -- the
rows are matched by exact name, and a name not in the table would simply match
nothing.

Only rows still marked 'External Services' are touched, and service_type only where
it is BLANK, following 0248/0250/0251/0266: an agent's manual correction in
Settings > Programs is never overwritten. That also makes a re-run a no-op.

Listed EXPLICITLY rather than by prefix, unlike 0266. The prefix "Home Accessibility
and Safety Modification - " covers 18 rows in our table but the file lists only 11;
matching on the prefix would silently convert seven programs nobody asked for.

DELIBERATELY LEFT EXTERNAL (in the table, absent from the file):
    Doors and Cabinet Handles - Brooklyn / Manhattan / Queens
    Kitchen Cabinet or Sinks  - Brooklyn / Manhattan / Queens
    Grab Bars                 - Queens

⚠️ That last one is very likely an omission in the source file rather than a
decision: Bathroom Facilities, Hand Rails and Non-skid Surfaces each appear for all
THREE boroughs, and Grab Bars appears for only two. It exists in our table. If it
should be internal, add it to PROGRAM_NAMES -- until then a Queens grab-bars case
will be REJECTED on import, because CaseSerializer refuses External Service cases.

Safe to run: zero cases currently reference any of the 11 names, so only future
imports are affected. As with 0262/0266 though, this IS what lets those cases in --
CaseSerializer rejects External Service cases outright.
"""
from django.db import migrations

CODE = "home_expense_assistance_repairs"

PREFIX = "Home Accessibility and Safety Modification - "

PROGRAM_NAMES = [
    PREFIX + name for name in (
        "Bathroom Facilities - Brooklyn",
        "Bathroom Facilities - Manhattan",
        "Bathroom Facilities - Queens",
        "Grab Bars - Brooklyn",
        "Grab Bars - Manhattan",
        "Hand Rails - Brooklyn",
        "Hand Rails - Manhattan",
        "Hand Rails - Queens",
        "Non-skid Surfaces - Brooklyn",
        "Non-skid Surfaces - Manhattan",
        "Non-skid Surfaces - Queens",
    )
]


def _rows(ActiveProgram):
    """Matched case-insensitively: the spreadsheet is hand-maintained, and a
    capitalisation difference must not silently skip a program."""
    from django.db.models import Q

    q = Q()
    for name in PROGRAM_NAMES:
        q |= Q(program_name__iexact=name)
    return ActiveProgram.objects.filter(q)


def seed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    rows = _rows(ActiveProgram)
    rows.filter(case_category="External Services").update(
        case_category="Internal Services", case_type="housing",
        main_category="Housing",
    )
    rows.filter(service_type="").update(service_type=CODE)


def unseed(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    rows = _rows(ActiveProgram)
    rows.filter(service_type=CODE).update(service_type="")
    rows.filter(case_category="Internal Services", case_type="housing").update(
        case_category="External Services", case_type="food", main_category="Food",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0267_dispatch_orders"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

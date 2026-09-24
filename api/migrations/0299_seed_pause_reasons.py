"""Seed the pause-reason catalogue.

The thirteen agreed reasons. Five are SYSTEM-owned and hidden from the agent's
picker, because an agent choosing them by hand would assert something a check has
not found:

    Insurance expired or invalid   the import eligibility gate
    Case Type Switch               a household -> individual switch on import
    Nutritionist Paused            the Nutritionist review drawer
    Out of Orbit                   the meal rule cannot fulfil the member
    Out of Range                   the delivery / primary ZIP is outside coverage

``sort_order`` puts the agent-selectable reasons first, roughly by how often they
actually occur in the 1,341 existing pause notes -- insurance 95, cancelled 89,
address 53, away 34, too much food 34 -- so the common answer is the near one.
Uncategorized is last: it is the fallback, and a fallback at the top of a list gets
chosen.

Idempotent on ``code``, so re-running against a database that already has some of
them updates the labels rather than duplicating.
"""
from django.db import migrations

REASONS = [
    # code, label, is_system, sort_order
    ("insurance_invalid",  "Insurance expired or invalid", True,  10),
    ("member_cancelled",   "Member cancelled",             False, 20),
    ("address_problem",    "Address problem",              False, 30),
    ("away_travelling",    "Away / traveling",             False, 40),
    ("too_much_food",      "Too much food",                False, 50),
    ("not_home",           "Not home",                     False, 60),
    ("delivery_issue",     "Delivery issue",               False, 70),
    ("pending_review",     "Pending review",               False, 80),
    ("case_type_switch",   "Case Type Switch",             True,  90),
    ("nutritionist_paused", "Nutritionist Paused",         True,  100),
    ("out_of_orbit",       "Out of Orbit",                 True,  110),
    ("out_of_range",       "Out of Range",                 True,  120),
    ("uncategorized",      "Uncategorized",                False, 999),
]


def seed(apps, schema_editor):
    PauseReason = apps.get_model("api", "PauseReason")

    created = 0
    for code, label, is_system, order in REASONS:
        _obj, made = PauseReason.objects.update_or_create(
            code=code,
            defaults={
                "label": label,
                "is_system": is_system,
                "sort_order": order,
                "is_active": True,
            },
        )
        created += 1 if made else 0
    print(f"\n  pause reasons: {created} created, {len(REASONS) - created} updated")


def unseed(apps, schema_editor):
    """Removes only the seeded codes.

    A reason an agent added later is theirs, and a reverse that deleted the whole
    table would take it -- along with the FK on every member profile citing it.
    """
    PauseReason = apps.get_model("api", "PauseReason")
    PauseReason.objects.filter(code__in=[r[0] for r in REASONS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0298_pause_reason"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

"""Seed the 'Nutritional Counseling' ticket type.

Adds a new agent-facing ticket category (active, so it shows in the New-Ticket
picker and the Work Queue filter). Idempotent via update_or_create.
"""

from django.db import migrations


CODE = "nutritional_counseling"
LABEL = "Nutritional Counseling"
DESCRIPTION = (
    "The member needs nutritional counseling / education follow-up "
    "(e.g. schedule, complete, or review a nutrition counseling session)."
)


def seed(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    TicketType.objects.update_or_create(
        code=CODE,
        defaults={
            "label": LABEL,
            "description": DESCRIPTION,
            "default_severity": "medium",
            "is_active": True,
        },
    )


def unseed(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    # Deactivate rather than delete so any tickets created against it survive.
    TicketType.objects.filter(code=CODE).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0138_alter_client_lifecycle_stage_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

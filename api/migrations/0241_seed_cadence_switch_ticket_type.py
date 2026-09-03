"""Seed the 'Cadence Switch' ticket type.

Adds a new agent-facing ticket category (active, so it shows in the New-Ticket
picker and the Work Queue filter). Idempotent via update_or_create.
"""

from django.db import migrations


CODE = "cadence_switch"
LABEL = "Cadence Switch"
DESCRIPTION = (
    "The member needs their delivery cadence changed "
    "(e.g. switch how often meals / boxes are delivered)."
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
        ("api", "0240_client_urgent_care_dismissed_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

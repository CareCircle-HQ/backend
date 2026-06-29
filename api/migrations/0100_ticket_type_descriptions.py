"""Activate the dedicated 'No Internal services Case' ticket type and fill in a
human-readable description for every ticket type (so agents see what each
category means in the admin/New-Ticket picker).

- ``case_no_services`` ('No Internal services Case') is re-activated and is now
  raised by the daily import when an internal-service case has no contracted
  services (previously folded into 'System Change Detected').
- ``description`` is populated for each known type. Descriptions are stored on
  the TicketType row so they can be tuned from the admin without a code change.
"""

from django.db import migrations


# code -> short description shown to agents.
DESCRIPTIONS = {
    "verification": "Member eligibility / enrollment needs to be verified before service can start.",
    "appointment": "Schedule, change, or follow up on an appointment with the member.",
    "service_change": "The member's service (meal type, cadence, quantity, etc.) needs to change.",
    "delivery_issue": "A problem with a delivery (late, damaged, not received) that needs follow-up.",
    "address_update": "The member's delivery address needs to be confirmed or updated.",
    "case_closure": "The member's case is closing; review and complete the service closure.",
    "food_complaint": "The member reported a problem with the quality or contents of their food.",
    "pause_service": "The member's service should be paused (travel, hospitalization, request, etc.).",
    "status_check": "Review the member's current status / eligibility and decide the next steps.",
    "login_problem": "The member can't access their account or portal and needs help signing in.",
    "cancellation": "The member requested to cancel their service.",
    "missing_wrong_order": "The member received a missing or incorrect order.",
    "case_no_services": (
        "An internal-service case has no contracted (internal) services attached, "
        "so the member has no active internal-services contract. Confirm whether a "
        "contract needs to be added before meal/box service can proceed."
    ),
    "system_change_detected": (
        "The daily Unite Us import detected a change on the member's record that an "
        "agent must review (see the ticket reason for the specific change)."
    ),
}


def seed(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    # Re-activate the dedicated 'No Internal services Case' type so it shows as
    # its own category in the Work Queue / picker.
    TicketType.objects.update_or_create(
        code="case_no_services",
        defaults={
            "label": "No Internal services Case",
            "default_severity": "medium",
            "is_active": True,
        },
    )
    # Fill descriptions (only touch the description field on existing rows).
    for code, description in DESCRIPTIONS.items():
        TicketType.objects.filter(code=code).update(description=description)


def unseed(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    TicketType.objects.filter(code="case_no_services").update(is_active=False)
    TicketType.objects.filter(code__in=list(DESCRIPTIONS)).update(description="")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0099_address_notes"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

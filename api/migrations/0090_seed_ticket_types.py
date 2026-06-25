"""Reseed TicketType to the new human-facing list + the system code.

- Seeds/updates the manual New-Ticket types as active.
- Adds ``system_change_detected`` (raised by the daily import / update feature)
  as **inactive** so it is hidden from the manual picker.
- Deactivates the legacy auto-pull types (no_active_insurance, case_closed, …)
  so they no longer appear in the picker. They are kept (not deleted) because
  historical tickets still point at them via a PROTECT FK.
"""

from django.db import migrations


# (code, label, default_severity, is_active)
NEW_TYPES = [
    ("verification", "Verification", "medium", True),
    ("appointment", "Appointment", "low", True),
    ("service_change", "Service Change", "medium", True),
    ("delivery_issue", "Delivery Issue", "high", True),
    ("address_update", "Address Update", "low", True),
    ("case_closure", "Case Closure", "medium", True),
    ("food_complaint", "Food Complaint", "high", True),
    ("pause_service", "Pause Service", "medium", True),
    ("status_check", "Status Check", "low", True),
    ("login_problem", "Login Problem", "high", True),
    ("cancellation", "Cancellation", "high", True),
    ("missing_wrong_order", "Missing / Wrong Order", "high", True),
    # System-raised; hidden from the manual picker.
    ("system_change_detected", "System Change Detected", "medium", False),
]

LEGACY_CODES = [
    "no_active_insurance",
    "insurance_expired",
    "no_active_coverage",
    "coverage_expired",
    "member_not_found",
    "case_closed",
    "authorization_changed",
    "case_no_services",
    "new_insurance",
    "new_coverage",
    "address_out_of_area",
    "credential_expired",
]


def seed(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    for code, label, severity, is_active in NEW_TYPES:
        TicketType.objects.update_or_create(
            code=code,
            defaults={
                "label": label,
                "default_severity": severity,
                "is_active": is_active,
            },
        )
    # Hide legacy auto-pull types from the picker (keep the rows for history).
    TicketType.objects.filter(code__in=LEGACY_CODES).update(is_active=False)


def unseed(apps, schema_editor):
    # Re-activate the legacy types; leave the new ones in place (harmless).
    TicketType = apps.get_model("api", "TicketType")
    TicketType.objects.filter(code__in=LEGACY_CODES).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0089_historicalticket_source_ticket_source"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

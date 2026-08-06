"""2026 ticket-category refresh.

- Ensures the KEEP list + the new agent-facing categories exist and are active.
- Keeps ``system_change_detected`` inactive (system-only; hidden from the picker).
- Removes (deactivates) Cancellation, Login Problem and No Internal services Case
  from the picker. TicketType rows are PROTECT-referenced by tickets, so they are
  deactivated (not deleted) to preserve history.
- Reassigns OPEN (non-resolved) tickets from Cancellation and No Internal services
  Case to Case Closure.
"""

from django.db import migrations

# (code, label, default_severity) -- seeded/updated as ACTIVE.
ACTIVE_TYPES = [
    ("address_update", "Address Update", "low"),
    ("appointment", "Appointment", "low"),
    ("case_closure", "Case Closure", "medium"),
    ("delivery_issue", "Delivery Issue", "high"),
    ("food_complaint", "Food Complaint", "high"),
    ("kitchen_switch", "Kitchen Switch", "medium"),
    ("missing_wrong_order", "Missing / Wrong Order", "high"),
    ("nutritional_counseling", "Nutritional Counseling", "medium"),
    ("pause_service", "Pause Service", "medium"),
    ("service_change", "Service Change", "medium"),
    ("status_check", "Status Check", "low"),
    ("verification", "Verification", "medium"),
    # New categories.
    ("ineligible_for_service", "Ineligible for Service", "medium"),
    ("meal_type_update", "Meal Type Update", "low"),
    ("somos_member", "SOMOS member", "low"),
    ("sipps_member", "SIPPS member", "low"),
    ("transferred_to_screening", "Transferred to Screening", "medium"),
    ("other", "Other", "low"),
]

# Removed from the picker (kept for history) + where their OPEN tickets go.
DEACTIVATE = ["cancellation", "login_problem", "case_no_services"]
MOVE_TO_CASE_CLOSURE = ["cancellation", "case_no_services"]
OPEN_STATUSES = ["open", "in_progress"]


def refresh(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    Ticket = apps.get_model("api", "Ticket")

    for code, label, severity in ACTIVE_TYPES:
        TicketType.objects.update_or_create(
            code=code,
            defaults={"label": label, "default_severity": severity, "is_active": True},
        )
    # System-only category stays hidden from the manual picker.
    TicketType.objects.filter(code="system_change_detected").update(is_active=False)

    # Move OPEN tickets off the removed categories onto Case Closure.
    case_closure = TicketType.objects.filter(code="case_closure").first()
    if case_closure is not None:
        Ticket.objects.filter(
            type__code__in=MOVE_TO_CASE_CLOSURE, status__in=OPEN_STATUSES,
        ).update(type=case_closure)

    # Remove the retired categories from the picker (rows kept for history).
    TicketType.objects.filter(code__in=DEACTIVATE).update(is_active=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0187_grandfather_remaining_verified"),
    ]

    operations = [
        migrations.RunPython(refresh, noop),
    ]

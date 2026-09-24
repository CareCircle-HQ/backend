"""Two ticket types for the internal-service rules.

Raised INSTEAD of a hold when the member is not yet in service. A hold is only
worth placing when something is actually being delivered; before that it costs
delivery cycles and protects nothing.

⚠ NEITHER REUSES ``ineligible_for_service``. These members ARE eligible -- the case
type is wrong, or the assessment does not name ECM. The remedy is to correct the
case, not to off-ramp the member, and folding them into a type that means "not
eligible at all" would corrupt what its 15 existing tickets mean.

The codes mirror the HoldReason codes deliberately, so the two vocabularies line up:
a member held for ``wrong_case_type`` and a member ticketed for ``wrong_case_type``
have the same underlying problem, caught at different points in their lifecycle.
"""
from django.db import migrations

TYPES = [
    (
        "wrong_case_type", "Wrong Case Type Opened",
        "The open case is not the service the latest eligibility assessment "
        "permits. Correct the case in Unite Us.",
    ),
    (
        "not_enhanced_member", "Not an Enhanced Member",
        "The latest eligibility assessment does not include Enhanced Care "
        "Management (Level 2), so the member is not entitled to internal services.",
    ),
]


def seed(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    created = updated = 0
    for order, (code, label, description) in enumerate(TYPES, 1):
        defaults = {"label": label, "is_active": True}
        # description / sort_order are optional on this model depending on the
        # schema version; set them only when they exist so the migration cannot
        # fail on a field that is not there.
        field_names = {f.name for f in TicketType._meta.get_fields()}
        if "description" in field_names:
            defaults["description"] = description
        if "sort_order" in field_names:
            defaults["sort_order"] = 900 + order
        _obj, was_created = TicketType.objects.update_or_create(
            code=code, defaults=defaults,
        )
        created += was_created
        updated += not was_created
    print(f"  ticket types: {created} created, {updated} updated")


def unseed(apps, schema_editor):
    # Deactivate rather than delete: a ticket already raised must keep rendering
    # its type.
    TicketType = apps.get_model("api", "TicketType")
    TicketType.objects.filter(code__in=[t[0] for t in TYPES]).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("api", "0302_hold_and_pause_dates")]
    operations = [migrations.RunPython(seed, unseed)]

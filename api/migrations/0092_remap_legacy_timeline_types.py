"""Remap legacy coarse timeline rows (event_type 'verification' / 'service') to
the new granular per-stage event types.

Existing rows stored the enrollment stage label in ``badge_text`` (e.g.
"Pending Verification", "Service Active") and the human title in ``title``. We
key off ``badge_text`` to assign the matching granular ``event_type`` + title so
the History tab reads each transition distinctly. Rows whose stage label we
can't recognise are left untouched (they stay valid under the retained legacy
enum members).
"""

from django.db import migrations

# EnrollmentStage label -> (new event_type, new title).
_LABEL_TO_GRANULAR = {
    "Pending Validation": ("pending_validation", "Pending Validation"),
    "Validated": ("validated", "Validated"),
    "Pending Verification": ("verification_requested", "Verification Requested"),
    "Verified": ("verification_completed", "Verification Completed"),
    "Waiting Authorization": ("waiting_authorization", "Waiting Authorization"),
    "Accepted": ("authorized", "Authorized"),
    "Denied": ("denied", "Denied"),
    "Kitchen Assignment": ("kitchen_assigned", "Kitchen Assigned"),
    "Service Active": ("service_activated", "Service Activated"),
    "Service Complete": ("service_completed", "Service Completed"),
    "On Hold": ("service_on_hold", "Service On Hold"),
    "Closed": ("service_closed", "Service Closed"),
    "Cancelled": ("service_cancelled", "Service Cancelled"),
}

_LEGACY_TYPES = ("verification", "service")


def forward(apps, schema_editor):
    TimelineEvent = apps.get_model("api", "TimelineEvent")
    for event in TimelineEvent.objects.filter(event_type__in=_LEGACY_TYPES):
        # A resume-from-hold row was titled "Service Resumed" while its stage
        # label is "Service Active"; preserve it as its own granular type.
        if event.title == "Service Resumed":
            event.event_type = "service_resumed"
            event.save(update_fields=["event_type"])
            continue
        mapping = _LABEL_TO_GRANULAR.get((event.badge_text or "").strip())
        if mapping is None:
            continue
        event.event_type, event.title = mapping
        event.save(update_fields=["event_type", "title"])


def backward(apps, schema_editor):
    """Best-effort reverse: collapse granular verification/service types back to
    the legacy coarse buckets so the migration is reversible."""
    TimelineEvent = apps.get_model("api", "TimelineEvent")
    verification = (
        "pending_validation", "validated", "verification_requested",
        "verification_completed", "waiting_authorization", "authorized", "denied",
    )
    service = (
        "kitchen_assigned", "service_activated", "service_on_hold",
        "service_resumed", "service_completed", "service_closed", "service_cancelled",
    )
    TimelineEvent.objects.filter(event_type__in=verification).update(event_type="verification")
    TimelineEvent.objects.filter(event_type__in=service).update(event_type="service")


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0091_alter_timelineevent_event_type"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

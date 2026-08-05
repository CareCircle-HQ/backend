from django.db import migrations
from django.utils import timezone


def grandfather(apps, schema_editor):
    """Apply the Nutritionist gate GOING FORWARD ONLY.

    Every enrollment already at or past Kitchen Assignment (Kitchen Assignment /
    Service Active / Service Complete) has, by definition, already cleared the
    point where the new Nutritionist sign-off gate sits -- so stamp it as
    nutritionist-approved. This keeps existing served/served-out households
    untouched (and lets a pull-back + re-approval re-advance them), while brand
    new verifications still flow through Pending Nutritionist.
    """
    EnrollmentVerification = apps.get_model("api", "EnrollmentVerification")
    now = timezone.now()
    EnrollmentVerification.objects.filter(
        stage__in=["kitchen_assignment", "service_active", "service_complete"],
        nutritionist_approved_at__isnull=True,
    ).update(nutritionist_approved_at=now)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0173_alter_timelineevent_event_type"),
    ]

    operations = [
        migrations.RunPython(grandfather, noop),
    ]

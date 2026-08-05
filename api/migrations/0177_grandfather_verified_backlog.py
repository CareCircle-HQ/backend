from datetime import datetime, timezone as dt_timezone

from django.db import migrations
from django.db.models import F


# The Nutritionist gate launched 2026-08-05. Every household verified BEFORE that
# (the production-snapshot backlog) was verified when no Nutritionist step
# existed, so -- like the served households grandfathered in 0174 -- they are
# retroactively marked approved. Verifications from launch day onward flow
# through Pending Nutritionist normally.
LAUNCH = datetime(2026, 8, 5, tzinfo=dt_timezone.utc)


def grandfather(apps, schema_editor):
    EnrollmentVerification = apps.get_model("api", "EnrollmentVerification")
    EnrollmentVerification.objects.filter(
        stage="verified",
        nutritionist_approved_at__isnull=True,
        verified_at__isnull=False,
        verified_at__lt=LAUNCH,
    ).update(nutritionist_approved_at=F("verified_at"))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0176_alter_memberdietaryprofile_medications"),
    ]

    operations = [
        migrations.RunPython(grandfather, noop),
    ]

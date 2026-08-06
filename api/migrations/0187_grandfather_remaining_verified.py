from django.db import migrations
from django.db.models import F
from django.utils import timezone


def grandfather(apps, schema_editor):
    """Catch-all grandfathering for the Nutritionist gate.

    0174/0177 grandfathered served households + the pre-launch verified backlog,
    but only those with a ``verified_at`` before the launch date. Any remaining
    VERIFIED household that was never Nutritionist-approved (e.g. legacy rows with
    a NULL verified_at, or ones outside the date window) is pre-feature backlog
    too -- mark it approved so it doesn't surface as Pending Nutritionist.
    Verifications created AFTER this runs get a NULL stamp and flow through the
    gate normally.
    """
    EnrollmentVerification = apps.get_model("api", "EnrollmentVerification")
    base = EnrollmentVerification.objects.filter(
        stage="verified", nutritionist_approved_at__isnull=True,
    )
    base.filter(verified_at__isnull=False).update(
        nutritionist_approved_at=F("verified_at")
    )
    base.filter(verified_at__isnull=True).update(
        nutritionist_approved_at=timezone.now()
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0186_memberdietaryprofile_nutritionist_pdf_key"),
    ]

    operations = [
        migrations.RunPython(grandfather, noop),
    ]

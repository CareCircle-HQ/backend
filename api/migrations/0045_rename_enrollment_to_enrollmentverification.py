from django.db import migrations


class Migration(migrations.Migration):
    """Rename Enrollment -> EnrollmentVerification and product_name -> program_name.

    Kept rename-only so the table and all referencing FKs (EnrollmentProcess,
    ServiceSchedule, StageEvent, TimelineEvent) are preserved. The new fields and
    the MemberVerification model are added by the following auto-generated migration.
    """

    dependencies = [
        ("api", "0044_client_is_level_historicalclient_is_level"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="Enrollment",
            new_name="EnrollmentVerification",
        ),
        migrations.RenameField(
            model_name="enrollmentverification",
            old_name="product_name",
            new_name="program_name",
        ),
    ]

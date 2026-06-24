from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0080_rename_api_memberv_enrollm_c906a6_idx_api_memberd_enrollm_70349f_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="client",
            name="lifecycle_stage",
            field=models.CharField(
                choices=[
                    ("inactive", "Inactive"),
                    ("consent", "Consent"),
                    ("screened", "Screened"),
                    ("assessment", "Assessment"),
                    ("navigation", "Navigation"),
                    ("pending_verification", "Pending Verification"),
                    ("verified", "Verified"),
                    ("waiting_authorization", "Waiting Authorization"),
                    ("authorized", "Authorized"),
                    ("kitchen_assignment", "Kitchen Assignment"),
                    ("active", "Active"),
                    ("completed", "Completed"),
                    ("not_eligible", "Not Eligible"),
                ],
                db_index=True,
                default="inactive",
                max_length=25,
            ),
        ),
        migrations.AlterField(
            model_name="enrollmentverification",
            name="stage",
            field=models.CharField(
                choices=[
                    ("pending_validation", "Pending Validation"),
                    ("validated", "Validated"),
                    ("pending_verification", "Pending Verification"),
                    ("verified", "Verified"),
                    ("waiting_authorization", "Waiting Authorization"),
                    ("authorized", "Accepted"),
                    ("denied", "Denied"),
                    ("kitchen_assignment", "Kitchen Assignment"),
                    ("service_active", "Service Active"),
                    ("service_complete", "Service Complete"),
                    ("closed", "Closed"),
                    ("on_hold", "On Hold"),
                    ("cancelled", "Cancelled"),
                ],
                db_index=True,
                default="pending_verification",
                max_length=25,
            ),
        ),
    ]

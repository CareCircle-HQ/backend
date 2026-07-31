from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0159_case_service_authorization_decision_detail"),
    ]

    operations = [
        migrations.AddField(
            model_name="case",
            name="service_authorization_denial_reason_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="case",
            name="service_authorization_denial_reason",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="case",
            name="authorized_units",
            field=models.CharField(blank=True, max_length=80),
        ),
    ]

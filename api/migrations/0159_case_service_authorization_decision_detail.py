from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0158_seed_active_programs"),
    ]

    operations = [
        migrations.AddField(
            model_name="case",
            name="service_authorization_decision_note",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="case",
            name="service_authorization_in_review_note",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="case",
            name="service_authorization_update_request_note",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="case",
            name="payer_authorization_number",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="case",
            name="service_authorization_submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="case",
            name="service_authorization_auto_approved",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="case",
            name="service_authorization_urgent",
            field=models.BooleanField(blank=True, null=True),
        ),
    ]

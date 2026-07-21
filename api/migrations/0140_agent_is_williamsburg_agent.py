from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0139_seed_nutritional_counseling_ticket_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="is_williamsburg_agent",
            field=models.BooleanField(default=False, db_index=True),
        ),
    ]

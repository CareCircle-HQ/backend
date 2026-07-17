from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0136_program_active_default_false"),
    ]

    operations = [
        migrations.AddField(
            model_name="programmaincategory",
            name="is_active",
            field=models.BooleanField(default=False),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0027_alter_client_call_transfer_answered'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='agent_name',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

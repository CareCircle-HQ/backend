from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0026_alter_address_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='client',
            name='call_transfer_answered',
            field=models.CharField(
                blank=True,
                choices=[
                    ('transfer_successful', 'Transfer Successful (Verification Agent Answered)'),
                    ('transfer_failed', 'Transfer Failed (No Answer)'),
                    ('no_verification_needed', 'No Verification Needed'),
                ],
                max_length=30,
            ),
        ),
    ]

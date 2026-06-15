from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0028_client_agent_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='phone_type',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='client',
            name='consent_status',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='client',
            name='consented_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='client',
            name='race',
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name='client',
            name='ethnicity',
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name='client',
            name='sexuality',
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name='client',
            name='preferred_spoken_language',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='client',
            name='preferred_written_language',
            field=models.CharField(blank=True, max_length=50),
        ),
    ]

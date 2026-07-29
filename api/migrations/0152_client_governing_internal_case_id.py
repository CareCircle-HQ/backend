from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0151_case_case_created_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='governing_internal_case_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='historicalclient',
            name='governing_internal_case_id',
            field=models.CharField(blank=True, max_length=64),
        ),
    ]

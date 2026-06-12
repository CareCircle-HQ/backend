from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0031_import_program_pipelines'),
    ]

    operations = [
        migrations.AlterField(
            model_name='case',
            name='service_type',
            field=models.CharField(blank=True, db_index=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='case',
            name='program_name',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

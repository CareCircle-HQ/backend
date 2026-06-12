from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0029_client_eform_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProgramPipeline',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('program_name', models.CharField(db_index=True, max_length=255, unique=True)),
                ('main_category', models.CharField(blank=True, max_length=120)),
                ('case_category', models.CharField(blank=True, db_index=True, max_length=120)),
                ('services_category', models.CharField(blank=True, max_length=120)),
                ('pipeline_name', models.CharField(blank=True, max_length=120)),
                ('pipeline_id', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['program_name'],
            },
        ),
        migrations.AddIndex(
            model_name='programpipeline',
            index=models.Index(fields=['case_category'], name='api_program_case_ca_idx'),
        ),
    ]

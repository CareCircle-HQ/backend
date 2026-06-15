# Generated manually for Agent model

from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0022_alter_address_options_alter_case_options_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='Agent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('agent_code', models.CharField(db_index=True, max_length=20, unique=True)),
                ('group', models.CharField(choices=[('Screeners', 'Screeners'), ('Verifiers', 'Verifiers'), ('Management', 'Management'), ('CS', 'CS')], default='Screeners', max_length=50)),
                ('status', models.CharField(default='Active', max_length=20)),
                ('cbo', models.CharField(blank=True, default='Met Council', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.AddIndex(
            model_name='agent',
            index=models.Index(fields=['agent_code', 'status'], name='api_agent_agent_c_8c91d6_idx'),
        ),
        migrations.AddIndex(
            model_name='agent',
            index=models.Index(fields=['group', 'status'], name='api_agent_group_s_6f9f68_idx'),
        ),
    ]

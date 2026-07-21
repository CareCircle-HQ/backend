"""Add the AllowedState allow-list and seed it with New York.

AllowedState is the list of US states we accept clients/cases from. Presence of
a row means the state is enabled; by default only NY is seeded (all other states
are disabled). Editable from Settings > Allowed States.
"""

import django.db.models.deletion
from django.db import migrations, models


def seed(apps, schema_editor):
    AllowedState = apps.get_model("api", "AllowedState")
    AllowedState.objects.get_or_create(code="NY", defaults={"name": "New York"})


def unseed(apps, schema_editor):
    AllowedState = apps.get_model("api", "AllowedState")
    AllowedState.objects.filter(code="NY").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0141_seed_kitchen_switch_ticket_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="AllowedState",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("code", models.CharField(db_index=True, max_length=2, unique=True)),
                ("name", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Allowed state",
                "verbose_name_plural": "Allowed states",
                "ordering": ["code"],
            },
        ),
        migrations.RunPython(seed, unseed),
    ]

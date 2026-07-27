from django.db import migrations, models


def backfill_case_created_at(apps, schema_editor):
    """Seed ``case_created_at`` for existing rows from ``date_opened``.

    ``date_opened`` was populated preferring the source case-created timestamp
    (CSV ``case_created_at`` / API ``created_at``) with a fallback to the
    agent-entered opened date, so it is the best available approximation for
    ordering until the NEXT import refreshes each row with the authoritative
    created timestamp. Rows with no ``date_opened`` stay null (sort last)."""
    Case = apps.get_model("api", "Case")
    Case.objects.filter(
        case_created_at__isnull=True, date_opened__isnull=False
    ).update(case_created_at=models.F("date_opened"))


def noop_reverse(apps, schema_editor):
    """Reverse of AddField drops the column, discarding the backfilled data."""


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0150_case_service_category_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='case',
            name='case_created_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='historicalcase',
            name='case_created_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_case_created_at, noop_reverse),
    ]

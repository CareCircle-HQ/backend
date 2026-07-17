"""Programs are now INACTIVE by default (opt-in).

Programs originate from Unite Us, but only a subset are actually served by this
org. Flip the ``Program.active`` default to False and, as a one-time backfill,
set every existing program inactive EXCEPT those offered by the primary
provider (``Met Council - SCN - PHS``), which stay active.
"""
from django.db import migrations, models

ACTIVE_PROVIDER_NAME = "Met Council - SCN - PHS"


def default_programs_inactive(apps, schema_editor):
    Program = apps.get_model("api", "Program")
    # Everything inactive first...
    Program.objects.update(active=False)
    # ...then re-activate the primary provider's programs.
    Program.objects.filter(provider__name=ACTIVE_PROVIDER_NAME).update(active=True)


def noop_reverse(apps, schema_editor):
    # Irreversible data backfill; leave rows as-is on reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0135_historicalticket_vip_ticket_vip"),
    ]

    operations = [
        migrations.AlterField(
            model_name="program",
            name="active",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(default_programs_inactive, noop_reverse),
    ]

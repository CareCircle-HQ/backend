"""Seed the UniteUsAgent allowlist from the snapshot taken on 2026-06-30.

The source files (Unite Us users export + CareCircle roster) are not committed
(the CSV is gitignored), so this migration bakes in the 180 classified agents
exported from the local DB. It lets prod populate the allowlist via ``migrate``
alone -- no files required.

Upsert is keyed by ``user_id`` so it's idempotent and safe to run on a database
that already has some/all of these rows (e.g. seeded via the management
command). The reverse is a no-op: we never delete agents on rollback, since the
table may legitimately contain rows added later from Settings.
"""

import json
import os

from django.db import migrations

_SEED_FILE = os.path.join(os.path.dirname(__file__), "0107_seed_unite_us_agents.json")

# Fields that are safe to refresh on an existing row from the snapshot.
_SYNC_FIELDS = (
    "employee_id",
    "first_name",
    "last_name",
    "name",
    "email",
    "work_title",
    "status",
    "is_us",
    "originating_team",
)


def seed(apps, schema_editor):
    UniteUsAgent = apps.get_model("api", "UniteUsAgent")
    with open(_SEED_FILE, "r", encoding="utf-8") as f:
        rows = json.load(f)

    for r in rows:
        user_id = r.get("user_id")
        if not user_id:
            continue
        defaults = {k: r.get(k) for k in _SYNC_FIELDS}
        UniteUsAgent.objects.update_or_create(user_id=user_id, defaults=defaults)


def unseed(apps, schema_editor):
    # Intentionally a no-op -- rollback must not wipe agents managed in Settings.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0106_uniteusagent_is_us_uniteusagent_originating_team"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

"""Composite index on api_case (client_id, case_type, date_opened DESC).

The Members list orders by a correlated subquery -- per client, the latest
internal-service case by date_opened -- which, combined with DISTINCT, runs that
subquery for every client (~60k) on each page. With only the client_id index it
scanned all of a client's cases, filtered case_type, and sorted date_opened;
after a large purge left api_case bloated this blew up to minutes and saturated
the DB. This composite index turns each subquery into a single lookup.

Created CONCURRENTLY (no table lock) and IF NOT EXISTS so it reconciles cleanly
with the index created ad-hoc during the incident.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("api", "0206_enrollmentverification_hidden_misinformation"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "api_case_client_type_dopen_idx "
                "ON api_case (client_id, case_type, date_opened DESC);"
            ),
            reverse_sql=(
                "DROP INDEX CONCURRENTLY IF EXISTS api_case_client_type_dopen_idx;"
            ),
            state_operations=[
                migrations.AddIndex(
                    model_name="case",
                    index=models.Index(
                        fields=["client", "case_type", "-date_opened"],
                        name="api_case_client_type_dopen_idx",
                    ),
                ),
            ],
        ),
    ]

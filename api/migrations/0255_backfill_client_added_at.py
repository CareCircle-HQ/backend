"""Backfill ``Client.client_added_at`` for rows that predate the field.

``client_added_at`` answers "when did this member reach OUR system?", which is
what the Data page's Member-Created range needs. It is stamped on first insert
going forward, so only existing rows need filling.

Best available evidence, in order:

1. the EARLIEST ``HistoricalClient`` row for that client -- simple-history writes
   one on the very first insert, so its ``history_date`` IS when we first saved
   the member;
2. failing that (history pruned, or rows created before history tracking),
   ``created_at`` -- the Unite Us date. Imprecise (Unite Us migrated ~80% of this
   population in over a few days in Dec-2024/Jan-2025), but it keeps the member
   inside SOME date window rather than dropping out of every filter.

``source`` is deliberately NOT backfilled: there is no reliable way to tell after
the fact which channel created a legacy row, and guessing would be worse than a
blank. It fills in naturally as members are next written.
"""

from django.db import migrations
from django.db.models import Min


def backfill(apps, schema_editor):
    Client = apps.get_model("api", "Client")
    HistoricalClient = apps.get_model("api", "HistoricalClient")

    # One aggregate query for the first-seen date of every client, rather than a
    # per-row lookup across ~69k members.
    first_seen = dict(
        HistoricalClient.objects.values_list("client_id")
        .annotate(first=Min("history_date"))
        .values_list("client_id", "first")
    )

    batch, total = [], 0
    qs = Client.objects.filter(client_added_at__isnull=True).only(
        "client_id", "client_added_at", "created_at"
    )
    for client in qs.iterator(chunk_size=2000):
        stamp = first_seen.get(client.client_id) or client.created_at
        if stamp is None:
            continue
        client.client_added_at = stamp
        batch.append(client)
        if len(batch) >= 2000:
            Client.objects.bulk_update(batch, ["client_added_at"])
            total += len(batch)
            batch = []
    if batch:
        Client.objects.bulk_update(batch, ["client_added_at"])
        total += len(batch)
    print(f"  backfilled client_added_at on {total} client(s)")


def unbackfill(apps, schema_editor):
    apps.get_model("api", "Client").objects.update(client_added_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0254_client_client_added_at_client_source_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]

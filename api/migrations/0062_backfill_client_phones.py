"""Backfill existing Client.client_phone_number values into ClientPhone.

The original ``Client.client_phone_number`` field is left untouched (still used
for display/sync); this just makes the Unite Us number searchable via the new
ClientPhone table, tagged source=uniteus / is_primary=True.
"""

from django.db import migrations


def _normalize(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def backfill(apps, schema_editor):
    Client = apps.get_model("api", "Client")
    ClientPhone = apps.get_model("api", "ClientPhone")

    to_create = []
    qs = Client.objects.exclude(client_phone_number="").only(
        "client_id", "client_phone_number", "phone_type"
    )
    for client in qs.iterator():
        normalized = _normalize(client.client_phone_number)
        if not normalized:
            continue
        # Idempotent: skip if this client already has the number.
        if ClientPhone.objects.filter(
            client_id=client.client_id, normalized=normalized
        ).exists():
            continue
        to_create.append(
            ClientPhone(
                client_id=client.client_id,
                raw=client.client_phone_number,
                normalized=normalized,
                label=(client.phone_type or ""),
                source="uniteus",
                is_primary=True,
            )
        )
    if to_create:
        ClientPhone.objects.bulk_create(to_create, batch_size=500)


def unbackfill(apps, schema_editor):
    ClientPhone = apps.get_model("api", "ClientPhone")
    ClientPhone.objects.filter(source="uniteus").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0061_clientphone_clientphone_uniq_client_phone_normalized"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]

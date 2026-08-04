"""Trigram GIN indexes for the Members-list omni-search.

The search matches name / email / phone / Medicaid ID / address with ``icontains``
(leading-wildcard ILIKE), which a normal btree index can't accelerate. pg_trgm's
GIN trigram indexes do.

Created with raw SQL (kept OUT of Django model state on purpose): the test DB is
built from model state with migrations disabled, so a model-level GinIndex would
make syncdb try to build a gin_trgm_ops index without pg_trgm present and fail.
Tests don't need these (icontains works without them); real DBs get them here.

Idempotent (IF NOT EXISTS). Requires privileges to CREATE EXTENSION on the target
DB (same as any pg_trgm use).
"""
from django.db import migrations

_INDEXES = [
    ("client_fname_trgm", "api_client", "first_name"),
    ("client_lname_trgm", "api_client", "last_name"),
    ("client_email_trgm", "api_client", "client_email_address"),
    ("client_phone_trgm", "api_client", "client_phone_number"),
    ("addr_street_trgm", "api_address", "street"),
    ("addr_city_trgm", "api_address", "city"),
    ("phone_normalized_trgm", "api_clientphone", "normalized"),
    ("ins_extmemberid_trgm", "api_insurance", "external_member_id"),
]

_forward = ["CREATE EXTENSION IF NOT EXISTS pg_trgm;"] + [
    f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" '
    f'USING gin ("{col}" gin_trgm_ops);'
    for name, table, col in _INDEXES
]
_reverse = [f'DROP INDEX IF EXISTS "{name}";' for name, _table, _col in _INDEXES]


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0169_reportexport"),
    ]

    operations = [
        migrations.RunSQL(sql="\n".join(_forward), reverse_sql="\n".join(_reverse)),
    ]

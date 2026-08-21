"""Backfill ``Client.internal_case_opened_at`` -- the denormalized Members-list
"Created" sort key (most recent internal-service case ``date_opened`` per client).

Set once here; kept fresh afterwards by ``reconcile_internal_service_authorization``.
Runs as two bulk UPDATEs (no per-row queries). DRY-RUN by default; ``--apply``
commits. Idempotent -- only rows whose value is out of date are touched.
"""
from django.core.management.base import BaseCommand
from django.db import connection

_SET_SQL = """
UPDATE api_client c
SET internal_case_opened_at = sub.mx
FROM (
    SELECT client_id, MAX(date_opened) AS mx
    FROM api_case
    WHERE case_type = 'internal_service'
    GROUP BY client_id
) sub
WHERE sub.client_id = c.client_id
  AND c.internal_case_opened_at IS DISTINCT FROM sub.mx;
"""

# Clients with no internal-service case must have a NULL sort key.
_CLEAR_SQL = """
UPDATE api_client c
SET internal_case_opened_at = NULL
WHERE c.internal_case_opened_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM api_case ca
      WHERE ca.client_id = c.client_id AND ca.case_type = 'internal_service'
  );
"""

_COUNT_SQL = """
SELECT
  (SELECT count(*) FROM api_client c
     JOIN (SELECT client_id, MAX(date_opened) mx FROM api_case
           WHERE case_type='internal_service' GROUP BY client_id) sub
       ON sub.client_id = c.client_id
    WHERE c.internal_case_opened_at IS DISTINCT FROM sub.mx) AS to_set,
  (SELECT count(*) FROM api_client c
    WHERE c.internal_case_opened_at IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM api_case ca
                      WHERE ca.client_id=c.client_id AND ca.case_type='internal_service')) AS to_clear;
"""


class Command(BaseCommand):
    help = "Backfill Client.internal_case_opened_at (Members-list sort key)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute(_COUNT_SQL)
            to_set, to_clear = cur.fetchone()
            self.stdout.write(f"Rows to set: {to_set} | rows to clear: {to_clear}")
            if not opts["apply"]:
                self.stdout.write(self.style.SUCCESS(
                    "DRY-RUN (no changes). Re-run with --apply."
                ))
                return
            cur.execute(_SET_SQL)
            set_n = cur.rowcount
            cur.execute(_CLEAR_SQL)
            clear_n = cur.rowcount
        self.stdout.write(self.style.SUCCESS(
            f"APPLIED: set {set_n} row(s), cleared {clear_n} row(s)."
        ))

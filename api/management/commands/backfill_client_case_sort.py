"""Backfill the two denormalized Members-list case-date keys:

* ``Client.internal_case_opened_at``           -- "Created" SORT key (most recent
  internal-service case ``date_opened``); set via two bulk UPDATEs.
* ``Client.governing_internal_case_opened_at`` -- "Created" FILTER key (the
  GOVERNING internal-service case ``date_opened``, favorability/deferral aware);
  set via a Python pass (governing selection isn't SQL-expressible).

Both are kept fresh afterwards by ``refresh_internal_case_sort`` during reconcile.
DRY-RUN by default; ``--apply`` commits. Idempotent -- only out-of-date rows change.
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
    help = "Backfill Client.internal_case_opened_at + governing_internal_case_opened_at."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        with connection.cursor() as cur:
            cur.execute(_COUNT_SQL)
            to_set, to_clear = cur.fetchone()
            self.stdout.write(f"[sort key] rows to set: {to_set} | rows to clear: {to_clear}")
            if apply:
                cur.execute(_SET_SQL)
                set_n = cur.rowcount
                cur.execute(_CLEAR_SQL)
                clear_n = cur.rowcount
                self.stdout.write(self.style.SUCCESS(
                    f"[sort key] APPLIED: set {set_n} row(s), cleared {clear_n} row(s)."
                ))
        # Governing "Created" filter key -- Python pass (favorability-based).
        self._backfill_governing(apply)
        if not apply:
            self.stdout.write(self.style.SUCCESS("\nDRY-RUN (no changes). Re-run with --apply."))

    def _backfill_governing(self, apply):
        from api.models import Client
        from api.portal.serializers import (
            active_enrollment,
            governing_service_case_for_display,
        )

        cols = [
            "governing_internal_case_opened_at",
            "governing_verification_requested_at",
            "governing_verification_completed_at",
        ]
        qs = Client.objects.prefetch_related(
            "cases",
            "enrollments",
            "household_membership__household__enrollment_verifications",
        )
        changed = 0
        checked = 0
        batch = []
        for c in qs.iterator(chunk_size=500):
            gov = governing_service_case_for_display(c)
            # No governing internal-service case -> "No Case": blank all
            # case/enrollment dates (mirrors the Data page's no-case blanking, so
            # a caseless enrollment doesn't leak a requested/completed date).
            if gov is None:
                vals = {col: None for col in cols}
            else:
                enr = active_enrollment(c)
                vals = {
                    "governing_internal_case_opened_at": gov.date_opened,
                    "governing_verification_requested_at": (
                        (enr.requested_at or enr.opened_at) if enr is not None else None
                    ),
                    "governing_verification_completed_at": enr.verified_at if enr is not None else None,
                }
            if any(getattr(c, col) != vals[col] for col in cols):
                for col in cols:
                    setattr(c, col, vals[col])
                batch.append(c)
            checked += 1
            if len(batch) >= 500:
                changed += len(batch)
                if apply:
                    Client.objects.bulk_update(batch, cols)
                batch = []
        if batch:
            changed += len(batch)
            if apply:
                Client.objects.bulk_update(batch, cols)
        verb = "APPLIED: updated" if apply else "would update"
        self.stdout.write(self.style.SUCCESS(
            f"[governing keys] {verb} {changed} of {checked} client(s)."
        ))

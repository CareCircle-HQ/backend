"""Backfill ``Case.added_to_system_at`` for existing rows.

The value is stamped on first insert going forward (see ``Case.save``); this
command populates historical rows from the earliest ``HistoricalCase``
history_date -- validated to equal the date the case was actually added to our
DB (the 158 cases a dump-diff proved were added on the 08/27 import all have
their earliest history_date on 08/27).

Runs in BATCHES (scoped by id) so each statement stays well under the prod
statement timeout and holds only a short lock -- a single UPDATE over all rows +
a GROUP BY across the whole history table times out. Idempotent: only fills NULL
``added_to_system_at`` and only rewrites a changed sort key, so it's safe to
re-run (e.g. after a timeout) to finish.

DRY-RUN by default; ``--apply`` commits.
"""
from django.core.management.base import BaseCommand
from django.db import connection

from api.models import Case, CaseType, Client


class Command(BaseCommand):
    help = "Backfill Case.added_to_system_at from the earliest HistoricalCase.history_date."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the update.")
        parser.add_argument(
            "--batch-size", type=int, default=2000,
            help="Rows per statement (default 2000).",
        )

    def handle(self, *args, **opts):
        batch = max(1, opts["batch_size"])
        todo = Case.objects.filter(added_to_system_at__isnull=True).count()
        self.stdout.write(f"cases missing added_to_system_at: {todo}")
        if not opts["apply"]:
            self.stdout.write("DRY-RUN -- re-run with --apply to commit.")
            return

        # 1) Case.added_to_system_at from the earliest history row, in batches of
        #    case_ids so the history GROUP BY is scoped (index on case_id) and each
        #    statement is short.
        case_ids = list(
            Case.objects.filter(added_to_system_at__isnull=True)
            .values_list("case_id", flat=True)
        )
        filled = 0
        for i in range(0, len(case_ids), batch):
            chunk = list(case_ids[i:i + batch])
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE api_case c
                    SET added_to_system_at = h.first_hist
                    FROM (
                        SELECT case_id, min(history_date) AS first_hist
                        FROM api_historicalcase
                        WHERE case_id = ANY(%s)
                        GROUP BY case_id
                    ) h
                    WHERE c.case_id = h.case_id AND c.added_to_system_at IS NULL
                    """,
                    [chunk],
                )
                filled += cur.rowcount
            self.stdout.write(f"  cases: {min(i + batch, len(case_ids))}/{len(case_ids)} scanned, {filled} filled")
        self.stdout.write(self.style.SUCCESS(f"APPLIED: filled {filled} case row(s)."))

        # 2) Refresh the denormalized Members-list SORT key (most-recent internal-
        #    service case added date), batched by client_id.
        client_ids = list(Client.objects.values_list("client_id", flat=True))
        seeded = 0
        for i in range(0, len(client_ids), batch):
            chunk = list(client_ids[i:i + batch])
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE api_client cl
                    SET internal_case_added_at = s.max_added
                    FROM (
                        SELECT client_id, max(added_to_system_at) AS max_added
                        FROM api_case
                        WHERE case_type = %s AND client_id = ANY(%s)
                        GROUP BY client_id
                    ) s
                    WHERE cl.client_id = s.client_id
                      AND cl.internal_case_added_at IS DISTINCT FROM s.max_added
                    """,
                    [CaseType.INTERNAL_SERVICE, chunk],
                )
                seeded += cur.rowcount
        self.stdout.write(self.style.SUCCESS(f"APPLIED: set sort key on {seeded} client(s)."))

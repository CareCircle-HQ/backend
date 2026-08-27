"""Backfill ``Case.added_to_system_at`` for existing rows.

The value is stamped on first insert going forward (see ``Case.save``); this
command populates historical rows from the earliest ``HistoricalCase``
history_date -- validated to equal the date the case was actually added to our
DB (the 158 cases a dump-diff proved were added on the 08/27 import all have
their earliest history_date on 08/27).

DRY-RUN by default; ``--apply`` commits. Idempotent -- only fills NULLs, so it
never overwrites a value already stamped on insert.
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Backfill Case.added_to_system_at from the earliest HistoricalCase.history_date."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the update.")

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM api_case WHERE added_to_system_at IS NULL")
            todo = cur.fetchone()[0]
            self.stdout.write(f"cases missing added_to_system_at: {todo}")
            if not opts["apply"]:
                self.stdout.write("DRY-RUN -- re-run with --apply to commit.")
                return
            cur.execute(
                """
                UPDATE api_case c
                SET added_to_system_at = h.first_hist
                FROM (
                    SELECT case_id, min(history_date) AS first_hist
                    FROM api_historicalcase
                    GROUP BY case_id
                ) h
                WHERE c.case_id = h.case_id AND c.added_to_system_at IS NULL
                """
            )
            self.stdout.write(self.style.SUCCESS(f"APPLIED: filled {cur.rowcount} row(s)."))

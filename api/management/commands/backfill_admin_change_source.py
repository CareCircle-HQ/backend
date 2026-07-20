"""Reclassify Client history CREATE rows misattributed as ``admin`` back to
``extension``.

Agents without a dialer extension have ``agent_code=None`` (see
``Agent.agent_code``). Before the attribution fix in ``api.history``, the
``_attribution_from_request`` helper keyed only on ``agent_code``, so those
code-less agents' extension writes fell through to the ADMIN branch and were
stamped ``change_source='admin'`` with ``change_actor='user:<agent_id>'``.

Those clients are genuinely EXTENSION creates. This command flips them back:
``change_source -> 'extension'`` and ``change_actor -> 'agent:<code-or-id>'`` --
but ONLY when the actor resolves to a real ``Agent``. Rows whose actor is a
genuine Django auth user (e.g. Django admin / superuser) are left as ``admin``.

It only touches the create row (``history_type='+'``), which is what drives the
Members page Source column. Idempotent: re-running finds nothing to change.

Dry-run unless ``--apply`` so you can review the counts first.

Usage:
    python manage.py backfill_admin_change_source
    python manage.py backfill_admin_change_source --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Agent, Client


class Command(BaseCommand):
    help = (
        "Reclassify Client history create rows misattributed as 'admin' (code-less "
        "agents' extension writes) back to 'extension'. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        apply = options["apply"]
        hist = Client.history.model

        # Resolve agent ids -> code once so we can (a) tell agent actors from
        # genuine Django users and (b) prefer the dialer code for the new actor.
        code_by_id = {
            str(i): c for i, c in Agent.objects.values_list("id", "agent_code")
        }

        rows = list(hist.objects.filter(history_type="+", change_source="admin"))
        to_fix = []
        left_admin = 0
        for row in rows:
            actor = row.change_actor or ""
            aid = actor[5:] if actor.startswith("user:") else ""
            if aid and aid in code_by_id:
                to_fix.append((row, aid))
            else:
                # Genuine (non-agent) admin write, or unrecognized actor: leave it.
                left_admin += 1

        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Backfill admin -> extension (Client create rows) ==="))
        self.stdout.write(f"  {'admin create rows':<34}: {len(rows)}")
        self.stdout.write(f"  {'resolvable to a real Agent':<34}: {len(to_fix)}")
        self.stdout.write(f"  {'left as admin (real Django user)':<34}: {left_admin}")

        if not apply:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: nothing written. Re-run with --apply to commit.")
            )
            return

        with transaction.atomic():
            for row, aid in to_fix:
                row.change_source = "extension"
                code = code_by_id.get(aid)
                row.change_actor = f"agent:{code or aid}"
                row.save(update_fields=["change_source", "change_actor"])

        remaining = hist.objects.filter(
            history_type="+", change_source="admin"
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"\nAPPLIED: reclassified {len(to_fix)} row(s); "
                f"remaining admin create rows: {remaining}."
            )
        )

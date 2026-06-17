"""Run the daily Unite Us data pull.

Invoke from cron at the configured time (default 02:00), e.g.:

    0 2 * * *  /path/to/.venv/bin/python /path/to/manage.py daily_pull

Options:
    --client-limit N   Only refresh the first N stored clients (smoke testing).
    --provider-id ID   Restrict to a single provider's credential.
    --triggered-by S   Label recorded on the ImportRun (default: cron).

Offline simulation (no live token / network) for debugging the full pipeline:
    python manage.py daily_pull --simulate
    python manage.py daily_pull --simulate --case-closed --no-insurance --no-coverage
    python manage.py daily_pull --simulate --cleanup   # remove seeded data after
Requires FIELD_ENCRYPTION_KEY to be set (the seeded credential's tokens are
encrypted at rest).
"""

from django.core.management.base import BaseCommand

from api.integrations.uniteus import config as uniteus_config
from api.services import uniteus_import
from api.services.uniteus_import import run_daily_pull


class Command(BaseCommand):
    help = "Run the daily Unite Us API pull (updates clients/cases, raises tickets)."

    def add_arguments(self, parser):
        parser.add_argument("--client-limit", type=int, default=None)
        parser.add_argument("--client-id", type=str, action="append", default=None,
                            help="Refresh only the given client id(s); repeatable.")
        parser.add_argument("--provider-id", type=str, default=None)
        parser.add_argument("--triggered-by", type=str, default="cron")
        # Simulation flags
        parser.add_argument("--simulate", action="store_true",
                            help="Run against canned fixtures via FakeUniteUsClient.")
        parser.add_argument("--case-closed", action="store_true")
        parser.add_argument("--no-insurance", action="store_true")
        parser.add_argument("--no-coverage", action="store_true")
        parser.add_argument("--cleanup", action="store_true",
                            help="Delete seeded simulation data after the run.")
        parser.add_argument("--force", action="store_true",
                            help="Run even when UNITEUS_ENABLED is false.")

    def handle(self, *args, **options):
        if options["simulate"]:
            return self._handle_simulate(options)
        if not uniteus_config.is_enabled() and not options["force"]:
            self.stdout.write(self.style.WARNING(
                "UNITEUS_ENABLED is false; skipping daily pull. "
                "Set UNITEUS_ENABLED=True (or pass --force) to run."
            ))
            return
        run = run_daily_pull(
            triggered_by=options["triggered_by"],
            client_limit=options["client_limit"],
            provider_id=options["provider_id"],
            client_ids=options["client_id"],
        )
        self._report(run)

    def _handle_simulate(self, options):
        from api.integrations.uniteus import simulation
        from api.models import Note, Ticket

        simulation.build_scenario(
            case_closed=options["case_closed"],
            with_insurance=not options["no_insurance"],
            with_coverage=not options["no_coverage"],
        )
        person_id = simulation.seed()
        self.stdout.write(self.style.WARNING(
            f"[simulate] seeded credential + client {person_id}; "
            f"swapping in FakeUniteUsClient"
        ))
        original = uniteus_import.uu_api.UniteUsClient
        uniteus_import.uu_api.UniteUsClient = simulation.FakeUniteUsClient
        try:
            run = run_daily_pull(triggered_by="simulate", client_ids=[person_id])
        finally:
            uniteus_import.uu_api.UniteUsClient = original

        self._report(run)
        tickets = Ticket.objects.filter(import_run=run)
        self.stdout.write(f"Tickets raised ({tickets.count()}):")
        for t in tickets:
            self.stdout.write(f"  - [{t.severity}] {t.type}: {t.reason}")
        self.stdout.write(
            f"Notes for client {person_id}: "
            f"{Note.objects.filter(client_id=person_id).count()} "
            f"(+case notes counted under their case)"
        )
        if options["cleanup"]:
            simulation.teardown()
            self.stdout.write(self.style.WARNING("[simulate] seeded data removed"))
        else:
            self.stdout.write(self.style.WARNING(
                "[simulate] data left in place; inspect in the admin, then re-run "
                "with --cleanup (re-running without cleanup is idempotent)."
            ))
        return

    def _report(self, run):
        self.stdout.write(
            self.style.SUCCESS(
                f"ImportRun {run.pk} {run.status}: "
                f"created={run.created_count} updated={run.updated_count} "
                f"skipped={run.skipped_count} errors={run.error_count}"
            )
        )
        if run.stats:
            self.stdout.write(f"Per-dataset: {run.stats}")
        if run.error_log:
            self.stdout.write(self.style.WARNING(run.error_log[:2000]))

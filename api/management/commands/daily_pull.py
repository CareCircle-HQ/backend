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

import fcntl
import os
import signal
import tempfile

from django.core.management.base import BaseCommand

from api.integrations.uniteus import config as uniteus_config
from api.services import uniteus_import
from api.services.uniteus_import import run_daily_pull

# Single-instance lock. The nightly cron fires daily, but a full live-API pull
# over the whole client base can take longer than 24h; without a lock the runs
# STACK (we found 8 concurrent pulls, several executing pre-deploy code in
# memory, which kept re-importing filtered-out cases and caused FK violations
# during cleanup). A non-blocking flock guarantees at most one pull at a time --
# a second invocation exits immediately instead of piling on. The lock lives in
# the system temp dir and auto-releases if the holder dies.
_LOCK_PATH = os.path.join(tempfile.gettempdir(), "carecircle_daily_pull.lock")


class _RunTimeout(Exception):
    """Raised when --max-runtime-seconds elapses so the run ends and releases the
    lock instead of wedging forever."""


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
        parser.add_argument(
            "--max-runtime-seconds", type=int, default=None,
            help=("Abort the pull after this many seconds so a wedged run can't "
                  "hold the singleton lock forever (default: env "
                  "DAILY_PULL_MAX_SECONDS, or unlimited)."),
        )
        parser.add_argument(
            "--ignore-lock", action="store_true",
            help="Run even if another daily_pull holds the singleton lock.",
        )

    def handle(self, *args, **options):
        if options["simulate"]:
            return self._handle_simulate(options)
        if not uniteus_config.is_enabled() and not options["force"]:
            self.stdout.write(self.style.WARNING(
                "UNITEUS_ENABLED is false; skipping daily pull. "
                "Set UNITEUS_ENABLED=True (or pass --force) to run."
            ))
            return

        # Acquire the singleton lock (unless explicitly overridden). Hold the fd
        # open for the whole run -- closing it releases the lock.
        lock_fd = None
        if not options["ignore_lock"]:
            lock_fd = self._acquire_lock()
            if lock_fd is None:
                self.stdout.write(self.style.WARNING(
                    "Another daily_pull is already running (lock held at "
                    f"{_LOCK_PATH}); skipping this run."
                ))
                return

        max_seconds = options["max_runtime_seconds"]
        if max_seconds is None:
            max_seconds = int(os.getenv("DAILY_PULL_MAX_SECONDS", "0") or 0)
        self._arm_timeout(max_seconds)
        try:
            run = run_daily_pull(
                triggered_by=options["triggered_by"],
                client_limit=options["client_limit"],
                provider_id=options["provider_id"],
                client_ids=options["client_id"],
            )
            self._report(run)
        except _RunTimeout:
            self.stdout.write(self.style.ERROR(
                f"daily_pull aborted after {max_seconds}s (--max-runtime-seconds)."
            ))
        finally:
            if max_seconds > 0:
                signal.alarm(0)
            self._release_lock(lock_fd)

    # -- singleton lock + runtime guard -----------------------------------
    def _acquire_lock(self):
        """Return an open, exclusively-locked file descriptor, or None if another
        process already holds it."""
        fd = open(_LOCK_PATH, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            return None
        fd.write(f"{os.getpid()}\n")
        fd.flush()
        return fd

    def _release_lock(self, fd):
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()

    def _arm_timeout(self, max_seconds):
        if max_seconds and max_seconds > 0:
            def _on_alarm(signum, frame):
                raise _RunTimeout()
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(max_seconds)

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

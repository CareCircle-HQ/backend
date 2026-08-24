"""Rebuild the EnrollmentAnalytics read model (Administration > Data page).

    python manage.py rebuild_enrollment_analytics            # full rebuild
    python manage.py rebuild_enrollment_analytics --prune    # + drop orphans

See docs/analytics-architecture.md (Phase 1).
"""

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "(Re)build the EnrollmentAnalytics denormalized read model."

    def add_arguments(self, parser):
        parser.add_argument("--prune", action="store_true",
                            help="Also delete analytics rows with no live enrollment.")

    def handle(self, *args, **opts):
        from api.services import enrollment_analytics as ea

        t = time.time()

        def progress(done, total):
            self.stdout.write(f"  {done}/{total} rebuilt…")

        n = ea.rebuild(progress=progress)
        pruned = ea.prune_orphans() if opts["prune"] else 0
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt {n} row(s)"
            + (f", pruned {pruned} orphan(s)" if opts["prune"] else "")
            + f" in {time.time() - t:.1f}s"
        ))

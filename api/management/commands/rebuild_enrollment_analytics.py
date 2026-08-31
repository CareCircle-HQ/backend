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
        parser.add_argument("--trigger", default="cli",
                            help="How the rebuild was launched (scheduled/manual/cli).")

    def handle(self, *args, **opts):
        from django.utils import timezone

        from api.models import AnalyticsRebuildRun
        from api.services import enrollment_analytics as ea

        t = time.time()
        run = AnalyticsRebuildRun.objects.create(trigger=opts["trigger"] or "cli")

        def progress(done, total):
            self.stdout.write(f"  {done}/{total} rebuilt…")

        n = ea.rebuild(progress=progress)
        pruned = ea.prune_orphans() if opts["prune"] else 0
        # Stamp completion so the Data page can show when the TASK last ran (not
        # the per-row refreshed_at watermark that live upserts bump).
        run.completed_at = timezone.now()
        run.rows = n
        run.pruned = pruned
        run.save(update_fields=["completed_at", "rows", "pruned"])
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt {n} row(s)"
            + (f", pruned {pruned} orphan(s)" if opts["prune"] else "")
            + f" in {time.time() - t:.1f}s"
        ))

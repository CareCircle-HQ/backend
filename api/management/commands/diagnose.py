"""One-command production health snapshot.

Written after the 2026-09-14 incident, where establishing "what is wrong right
now" took a dozen ad-hoc shell queries pasted back and forth. Everything here is
read-only and cheap, so it is safe to run on production during an incident.

The output is deliberately compact and plain-text: its main job is to be PASTED
somewhere (a ticket, a chat with someone who has no server access) and still make
sense.

    python manage.py diagnose
    python manage.py diagnose --hours 6
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone


class Command(BaseCommand):
    help = "Print a compact production health snapshot (read-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours", type=int, default=3,
            help="Look-back window for runs and recent activity (default 3).",
        )

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(hours=opts["hours"])
        self.stdout.write(
            f"=== diagnose @ {timezone.now():%Y-%m-%d %H:%M:%S} UTC "
            f"(last {opts['hours']}h) ==="
        )
        for section in (
            self._imports, self._uniteus, self._delivery_gaps, self._read_model,
        ):
            try:
                section(since)
            except Exception as exc:  # noqa: BLE001 - one broken section must
                # not hide the others; that is the whole point of a snapshot.
                self.stdout.write(f"  ERROR in {section.__name__}: {exc}")

    # -- sections ----------------------------------------------------------
    def _imports(self, since):
        """Import runs, and specifically ones stuck RUNNING.

        A run is set RUNNING up front and only resolved in `finalize()`, so a
        killed process leaves the row RUNNING for ever -- which is how a stale row
        can permanently block a feature (the "Prepare Members for PO" job).
        """
        from api.models import ImportRun, ImportRunStatus

        self.stdout.write("\n-- Import runs --")
        stuck = ImportRun.objects.filter(status=ImportRunStatus.RUNNING)
        old_stuck = stuck.filter(started_at__lt=timezone.now() - timedelta(hours=2))
        self.stdout.write(f"  RUNNING now              : {stuck.count()}")
        self.stdout.write(
            f"  RUNNING > 2h (likely dead): {old_stuck.count()}"
            + ("   <-- these block features until closed" if old_stuck else "")
        )
        for r in old_stuck.order_by("started_at")[:5]:
            self.stdout.write(
                f"      #{r.pk} {r.source} started {r.started_at:%m-%d %H:%M} "
                f"by {r.triggered_by}"
            )
        recent = ImportRun.objects.filter(started_at__gte=since).order_by("-started_at")
        self.stdout.write(f"  started in window        : {recent.count()}")
        for r in recent[:8]:
            secs = (r.finished_at - r.started_at).total_seconds() if r.finished_at else None
            flag = "  <-- SLOW" if (secs or 0) > 60 else ""
            self.stdout.write(
                f"      {r.started_at:%H:%M:%S} {str(round(secs) if secs else '-'):>6}s "
                f"{r.status:10} {r.triggered_by}{flag}"
            )

    def _uniteus(self, since):
        """Credential pool. A large ACTIVE pool used to be a performance problem:
        on-demand refreshes walked every one of them (fixed 2026-09-14)."""
        from api.models import UniteUsCredential

        self.stdout.write("\n-- Unite Us credentials --")
        by_status = (
            UniteUsCredential.objects.values("status")
            .annotate(n=Count("pk")).order_by("-n")
        )
        for row in by_status:
            self.stdout.write(f"  {row['status']:12} {row['n']}")
        fresh = UniteUsCredential.objects.filter(
            status="active", last_captured_at__gte=timezone.now() - timedelta(days=7),
        ).count()
        self.stdout.write(f"  captured < 7d ago: {fresh}")

    def _delivery_gaps(self, since):
        """Households that look active but cannot receive a delivery -- the
        "activated but no plan" family (see docs/findings-activated-no-plan.md)."""
        from api.models import (
            EnrollmentStage, EnrollmentVerification, ScheduleStatus,
            SERVICE_EXCLUDED_MEMBER_STATUSES,
        )

        self.stdout.write("\n-- Delivery gaps --")
        stranded = list(
            EnrollmentVerification.objects
            .filter(stage=EnrollmentStage.SERVICE_ACTIVE, kitchen__isnull=False)
            .exclude(delivery_schedules__status=ScheduleStatus.SCHEDULED)
            .distinct().prefetch_related("member_profiles")[:500]
        )
        servable = sum(
            1 for e in stranded
            if any(p.status not in SERVICE_EXCLUDED_MEMBER_STATUSES
                   for p in e.member_profiles.all())
        )
        self.stdout.write(f"  service_active + kitchen, no live plan: {len(stranded)}")
        self.stdout.write(
            f"      of those, WITH a servable member    : {servable}"
            + ("   <-- fixable by a calendar rebuild" if servable else "")
        )
        self.stdout.write(
            f"      of those, nobody servable           : {len(stranded) - servable}"
            "   (needs a member returned to service)"
        )

    def _read_model(self, since):
        """Analytics freshness + the company-status spread the Data page shows."""
        from django.db.models import Max

        from api.models import EnrollmentAnalytics

        self.stdout.write("\n-- Read model --")
        refreshed = EnrollmentAnalytics.objects.aggregate(t=Max("refreshed_at"))["t"]
        if refreshed:
            age = (timezone.now() - refreshed).total_seconds() / 3600
            self.stdout.write(
                f"  rows {EnrollmentAnalytics.objects.count()}, newest refresh "
                f"{refreshed:%m-%d %H:%M} ({age:.1f}h ago)"
            )
        for row in (
            EnrollmentAnalytics.objects.values("company_status")
            .annotate(n=Count("pk")).order_by("-n")
        ):
            self.stdout.write(f"  {row['company_status'] or '(blank)':12} {row['n']}")

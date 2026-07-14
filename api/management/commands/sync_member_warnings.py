"""Refresh the member/household warning snapshot for every servable household.

The warning snapshot (``MemberWarning``) is kept current on the fly -- a live
scan when a profile is opened, and a sync on extension case saves / CSV imports.
This nightly sweep is the safety net for TIME-BASED checks that no write would
otherwise re-trigger (e.g. an insurance or internal-service authorization that
simply lapses with the passing of a day).

    python manage.py sync_member_warnings           # sweep all servable households
    python manage.py sync_member_warnings --limit 100

Detection logic lives in ``api.services.warnings`` (the rule registry); this
command only drives :func:`sync_household_warnings` across households.
"""

from django.core.management.base import BaseCommand

from api.models import EnrollmentVerification
from api.services.warnings import _INACTIVE_STAGES, sync_household_warnings


class Command(BaseCommand):
    help = "Refresh the member/household warning snapshot for all households."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Cap the number of enrollments processed (for testing).",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        qs = (
            EnrollmentVerification.objects
            .exclude(stage__in=_INACTIVE_STAGES)
            .order_by("-opened_at")
        )
        total = qs.count()
        if limit:
            qs = qs[:limit]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Syncing warnings for {min(total, limit) if limit else total} "
            f"of {total} servable enrollments"
        ))

        processed = failed = 0
        for enr in qs.iterator():
            try:
                sync_household_warnings(enr)
                processed += 1
            except Exception as exc:  # noqa: BLE001 - keep sweeping on a bad row
                failed += 1
                self.stderr.write(f"  ! enrollment {enr.pk}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"Done. Synced {processed} households"
            + (f", {failed} failed" if failed else "") + "."
        ))

"""Bulk-dismiss clients from the Urgent Care ("Need Attention") list.

The Urgent Care list is exactly the clients flagged ``Client.is_new=True`` who
have NOT yet entered the verification pipeline -- i.e. no enrollment of their
own AND none on their household (mirrors the ``scope=need_attention`` query in
``api.portal.views_members.MembersListView``). "Dismissing" a client just
clears ``is_new`` so they drop off the list.

Two selection modes (both write ONLY the flag -- no audit Note and no timeline
event, unlike the per-member dismiss endpoint):

  * default    -- is_new clients on the list who have NO internal-service case.
  * --verified -- is_new clients whose verification is already COMPLETE (a
                  governing enrollment, their own or their household's, has
                  ``verified_at`` set). These are stale flags that should have
                  cleared on verification; this sweeps them off the list.

Preview first (default), then commit:

    python manage.py dismiss_urgent_care                     # dry run (no-case mode)
    python manage.py dismiss_urgent_care --apply             # commit no-case mode
    python manage.py dismiss_urgent_care --verified          # dry run (verified mode)
    python manage.py dismiss_urgent_care --verified --apply  # commit verified mode
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from api.models import CaseType, Client


class Command(BaseCommand):
    help = (
        "Clear is_new for Urgent Care ('Need Attention') clients that have no "
        "internal-service case. Dry-run by default; pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--verified", action="store_true",
            help="Target is_new clients whose verification is already complete "
                 "(instead of the default: list members with no internal-service case).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually clear is_new. Without this the command only previews.",
        )
        parser.add_argument(
            "--limit", type=int, default=50,
            help="Max rows to print in the preview (default 50). Counts are always full.",
        )

    def _queryset(self, verified):
        if verified:
            # is_new clients already verified: a governing enrollment (own or
            # household) has verified_at set. The flag should have cleared on
            # verification; this sweeps the stragglers.
            verified_q = (
                Q(enrollments__verified_at__isnull=False)
                | Q(household_membership__household__enrollment_verifications__verified_at__isnull=False)
            )
            return Client.objects.filter(is_new=True).filter(verified_q).distinct()
        # Mirror the Need Attention list: is_new AND no enrollment (own or
        # household), then narrow to those with no internal-service case.
        return (
            Client.objects.filter(is_new=True)
            .exclude(
                Q(enrollments__isnull=False)
                | Q(household_membership__household__enrollment_verifications__isnull=False)
            )
            .exclude(cases__case_type=CaseType.INTERNAL_SERVICE)
            .distinct()
        )

    def handle(self, *args, **opts):
        verified = opts["verified"]
        cohort = (
            "is_new client(s) already verified"
            if verified
            else "Urgent Care client(s) with no internal-service case"
        )
        qs = self._queryset(verified)
        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                f"No {cohort}. Nothing to do."
            ))
            return

        limit = opts["limit"]
        self.stdout.write(f"{total} {cohort}:")
        for cid, first, last in qs.values_list(
            "client_id", "first_name", "last_name"
        )[:limit]:
            self.stdout.write(f"  {cid}  {first} {last}".rstrip())
        if total > limit:
            self.stdout.write(f"  ... and {total - limit} more")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to clear is_new."
            ))
            return

        # Bulk flag-only write: no Note, no timeline event.
        updated = Client.objects.filter(
            pk__in=list(qs.values_list("pk", flat=True))
        ).update(is_new=False)
        self.stdout.write(self.style.SUCCESS(
            f"Dismissed {updated} client(s) from the Urgent Care list (is_new cleared)."
        ))

"""Bulk-dismiss clients from the Urgent Care ("Need Attention") list.

The list shows every ELIGIBLE member with an OPEN internal-service (meal/box)
case and NO enrollment yet -- valid Medicaid + social care, not on the
not_eligible/ineligible off-ramp (mirrors ``scope=need_attention`` in
``api.portal.views_members.MembersListView``). "Dismissing" sets
``Client.urgent_care_dismissed=True`` so they drop off the list (independent of
``is_new``, which no longer gates the list).

Preview first (default), then commit:

    python manage.py dismiss_urgent_care            # dry run
    python manage.py dismiss_urgent_care --apply    # commit
"""
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef, Q

from api.models import Case, CaseStatus, CaseType, Client, ClientStage
from api.services.lifecycle import valid_medicaid_exists, valid_social_care_exists


class Command(BaseCommand):
    help = (
        "Dismiss every member currently on the Urgent Care list "
        "(sets urgent_care_dismissed). Dry-run by default; --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually dismiss. Without this the command only previews.",
        )
        parser.add_argument(
            "--limit", type=int, default=50,
            help="Max rows to print in the preview (default 50). Counts are full.",
        )

    def _list_queryset(self):
        """The current Urgent Care list -- mirrors the need_attention scope."""
        open_ic = Case.objects.filter(
            client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
        ).exclude(case_status__in=[CaseStatus.CLOSED, CaseStatus.CANCELLED])
        return (
            Client.objects.filter(Exists(open_ic))
            .filter(valid_medicaid_exists(), valid_social_care_exists())
            .exclude(
                Q(enrollments__isnull=False)
                | Q(household_membership__household__enrollment_verifications__isnull=False)
            )
            .exclude(lifecycle_stage__in=[ClientStage.NOT_ELIGIBLE, ClientStage.INELIGIBLE])
            .exclude(urgent_care_dismissed=True)
            .distinct()
        )

    def handle(self, *args, **opts):
        qs = self._list_queryset()
        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "Urgent Care list is empty. Nothing to dismiss."
            ))
            return

        limit = opts["limit"]
        self.stdout.write(f"{total} member(s) currently on the Urgent Care list:")
        for cid, first, last in qs.values_list(
            "client_id", "first_name", "last_name"
        )[:limit]:
            self.stdout.write(f"  {cid}  {first} {last}".rstrip())
        if total > limit:
            self.stdout.write(f"  ... and {total - limit} more")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to dismiss."
            ))
            return

        updated = Client.objects.filter(
            pk__in=list(qs.values_list("pk", flat=True))
        ).update(urgent_care_dismissed=True)
        self.stdout.write(self.style.SUCCESS(
            f"Dismissed {updated} client(s) from the Urgent Care list."
        ))

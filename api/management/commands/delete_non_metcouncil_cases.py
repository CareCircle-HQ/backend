"""Delete cases that do NOT belong to Met Council.

A case belongs to Met Council under the UNION rule (the same gate every import
path now enforces, see ``api.services.lifecycle.is_met_council_case``): Met
Council either CREATED it (``originating_provider_id`` == the Met Council id) OR
MANAGES/services it (``provider_id`` == the id, or ``provider_name`` ==
"Met Council - SCN - PHS"). Every other case is an external-org case that
shouldn't be in our member base -- this command removes the ones already
imported before the filter existed.

Dry-run by default (prints a breakdown of what WOULD be deleted); pass --apply
to actually delete. Deleting a case cascades to its child rows (contracted
services, etc.) per the model's on_delete; affected clients' funnel stages are
recomputed afterwards so the member view stays consistent.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from api.models import Case, Client
from api.services.lifecycle import (
    MET_COUNCIL_PROVIDER_ID,
    MET_COUNCIL_PROVIDER_NAME,
)


class Command(BaseCommand):
    help = (
        "Delete cases that don't belong to Met Council (union of originating + "
        "managing provider). Dry-run by default; pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without this the command only previews.",
        )
        parser.add_argument(
            "--limit", type=int, default=20,
            help="How many sample rows to print in the preview (default 20).",
        )

    def _doomed_queryset(self):
        """Cases with NO Met Council signal (the complement of the keep rule)."""
        keep = (
            Q(originating_provider_id=MET_COUNCIL_PROVIDER_ID)
            | Q(provider_id=MET_COUNCIL_PROVIDER_ID)
            | Q(provider_name__iexact=MET_COUNCIL_PROVIDER_NAME)
        )
        return Case.objects.exclude(keep)

    def handle(self, *args, **opts):
        qs = self._doomed_queryset()
        total_cases = Case.objects.count()
        n = qs.count()

        if n == 0:
            self.stdout.write(self.style.SUCCESS(
                f"No non-Met Council cases found ({total_cases} cases, all Met Council)."
            ))
            return

        self.stdout.write(
            f"{n} of {total_cases} case(s) are NOT Met Council and would be deleted."
        )
        # Breakdown by managing organization so it's clear what's being removed.
        by_org = Counter()
        for name in qs.values_list("provider_name", flat=True):
            by_org[(name or "(blank)")] += 1
        self.stdout.write("  By managing organization (provider_name):")
        for name, c in by_org.most_common(opts["limit"]):
            self.stdout.write(f"    {c:6}  {name}")
        extra = len(by_org) - opts["limit"]
        if extra > 0:
            self.stdout.write(f"    ... and {extra} more organization(s)")

        affected_clients = qs.values_list("client_id", flat=True).distinct().count()
        self.stdout.write(f"  Spanning {affected_clients} client(s).")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to delete."
            ))
            return

        client_ids = list(qs.values_list("client_id", flat=True).distinct())
        with transaction.atomic():
            deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {n} non-Met Council case(s) ({deleted} row(s) incl. children)."
        ))

        # Recompute the acquisition funnel for every client that lost a case, so
        # a member no longer backed by a Met Council case reflects it. Best-effort
        # per client -- one failure never aborts the sweep.
        from api.services.lifecycle import recompute_client_stage

        recomputed = 0
        for client in Client.objects.filter(pk__in=client_ids):
            try:
                recompute_client_stage(client)
                recomputed += 1
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  stage recompute failed for {client.pk}: {exc}")
        self.stdout.write(self.style.SUCCESS(
            f"Recomputed lifecycle stage for {recomputed} affected client(s)."
        ))

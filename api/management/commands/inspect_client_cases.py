"""Inspect a single client's cases and show the Met Council signal for each.

For the given client, prints every case with the fields that drive the
``is_met_council_case`` union rule (originating provider, managing provider,
provider name) plus the derived flag, so you can reconcile each case against
what Unite Us shows for that member.

Usage:
    python manage.py inspect_client_cases <client_id> [<client_id> ...]
"""
from django.core.management.base import BaseCommand

from api.models import Case, Client
from api.services.lifecycle import case_is_met_council


class Command(BaseCommand):
    help = "Show a client's cases with their Met Council signal + provider fields."

    def add_arguments(self, parser):
        parser.add_argument("client_ids", nargs="+", help="One or more client UUIDs.")

    def handle(self, *args, **opts):
        for cid in opts["client_ids"]:
            client = Client.objects.filter(pk=cid).first()
            head = self.style.MIGRATE_HEADING
            self.stdout.write(head(f"\n=== Client {cid} ==="))
            if client is None:
                self.stdout.write(self.style.ERROR("  client not found"))
                continue
            self.stdout.write(f"  name: {client.first_name} {client.last_name}")

            cases = Case.objects.filter(client_id=cid).order_by("-date_opened")
            if not cases:
                self.stdout.write("  (no cases)")
                continue

            mc = non_mc = 0
            for c in cases:
                is_mc = case_is_met_council(c)
                mc += is_mc
                non_mc += not is_mc
                tag = self.style.SUCCESS("MET COUNCIL") if is_mc else self.style.WARNING("NON-MC    ")
                self.stdout.write(
                    f"\n  [{tag}] {c.case_type} / {c.case_status}"
                )
                self.stdout.write(f"    case_id            : {c.case_id}")
                self.stdout.write(f"    program            : {c.program_name or '—'}")
                self.stdout.write(f"    service_type       : {c.service_type or '—'}")
                self.stdout.write(
                    f"    originating_provider: {c.originating_provider_id or '—'} "
                    f"| {c.originating_provider_name or '—'}"
                )
                self.stdout.write(
                    f"    managing provider   : {c.provider_id or '—'} "
                    f"| {c.provider_name or '—'}"
                )
                self.stdout.write(f"    date_opened        : {c.date_opened}")

            self.stdout.write(
                head(f"\n  Summary: {cases.count()} case(s) — {mc} Met Council, {non_mc} non-MC")
            )

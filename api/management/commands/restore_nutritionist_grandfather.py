"""Restore (re-apply) grandfathered nutritionist approval for specific client(s).

The inverse of ``revert_nutritionist_grandfather``: re-stamps
``nutritionist_approved_at`` (= verified_at, else now) on the client's VERIFIED
enrollment(s) that have no approval yet, so the household clears the Nutritionist
gate again and returns to exactly where it was (Waiting Authorization, or Waiting
for Kitchen Assignment once authorization is approved). Never sets a reviewer.

    python manage.py restore_nutritionist_grandfather <client_id> [<client_id> ...] [--dry-run]
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Re-apply grandfathered nutritionist approval for given client(s)."

    def add_arguments(self, parser):
        parser.add_argument("client_ids", nargs="+", help="Client UUID(s).")
        parser.add_argument("--dry-run", action="store_true", help="Report only; make no changes.")

    def handle(self, *args, **opts):
        from api.models import Client, EnrollmentVerification
        from api.services.lifecycle import recompute_client_stage

        dry = opts["dry_run"]
        for cid in opts["client_ids"]:
            client = Client.objects.filter(client_id=cid).first()
            if client is None:
                self.stdout.write(self.style.WARNING(f"{cid}: client not found"))
                continue
            qs = EnrollmentVerification.objects.filter(
                client=client,
                stage="verified",
                nutritionist_approved_at__isnull=True,
                nutritionist_approved_by__isnull=True,
            )
            n = qs.count()
            self.stdout.write(f"{cid}: {n} verified enrollment(s) -> grandfathered approved")
            if dry or n == 0:
                continue
            for e in qs:
                e.nutritionist_approved_at = e.verified_at or timezone.now()
                e.save(update_fields=["nutritionist_approved_at"])
            recompute_client_stage(client)
            client.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(f"  done; client lifecycle: {client.lifecycle_stage}"))

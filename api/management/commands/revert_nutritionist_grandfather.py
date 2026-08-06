"""Revert GRANDFATHERED nutritionist approval for specific client(s).

Grandfathering (migrations 0174/0177/0187) stamped ``nutritionist_approved_at``
on pre-feature verified households WITHOUT a real reviewer
(``nutritionist_approved_by`` is null). This one-off clears that stamp for the
given client(s) so their verified enrollment(s) go back to Pending Nutritionist
for an actual review. REAL sign-offs (approved_by set) are never touched.

    python manage.py revert_nutritionist_grandfather <client_id> [<client_id> ...] [--dry-run]
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Clear grandfathered (approved_by=null) nutritionist approval for given client(s)."

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
                nutritionist_approved_at__isnull=False,
                nutritionist_approved_by__isnull=True,   # grandfathered only
            )
            n = qs.count()
            self.stdout.write(f"{cid}: {n} grandfathered enrollment(s) -> Pending Nutritionist")
            if dry or n == 0:
                continue
            qs.update(
                nutritionist_approved_at=None,
                nutritionist_signature="",
                nutritionist_signature_image="",
            )
            recompute_client_stage(client)
            client.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(f"  done; client lifecycle: {client.lifecycle_stage}"))

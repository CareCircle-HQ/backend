"""One-time backfill: set imported clients' funnel stage to Pending Verification.

Context: a bulk import of Unite Us data created internal-service (meals/boxes)
cases carrying the Unite Us authorization status ``approved``. The funnel
derivation (``api.services.lifecycle._case_authorization_stage``) maps that to
``AUTHORIZED``, so thousands of clients show as Authorized even though CareCircle
has not run its own verification. This command parks those clients at
``pending_verification`` so they appear in the verification queue.

Target: clients that have >=1 internal-service case and NO EnrollmentVerification
(clients with a real enrollment are governed by it and are left untouched).

    python manage.py set_pending_verification              # DRY RUN (no writes)
    python manage.py set_pending_verification --apply       # apply the update
    python manage.py set_pending_verification --authorized-only --apply

IMPORTANT: this only flips the *derived* ``Client.lifecycle_stage`` field via a
bulk update (no StageEvent, no signals). It does NOT change the derivation, so a
later ``recompute_client_stage`` (daily sync or any client edit) will re-derive
``authorized`` and revert these rows. Make the derivation/enrollment fix to make
it permanent.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from api.models import CaseType, Client, ClientStage


class Command(BaseCommand):
    help = (
        "Backfill clients with an internal-service case (and no enrollment) to "
        "lifecycle_stage=pending_verification. Dry-run unless --apply is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the change. Without this flag the command only reports.",
        )
        parser.add_argument(
            "--authorized-only",
            action="store_true",
            help="Restrict to clients currently derived as 'authorized'.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        authorized_only = options["authorized_only"]
        head = self.style.MIGRATE_HEADING

        target = Client.objects.filter(
            cases__case_type=CaseType.INTERNAL_SERVICE,
            enrollments__isnull=True,
        )
        if authorized_only:
            target = target.filter(lifecycle_stage=ClientStage.AUTHORIZED)

        # Distinct client ids (the case join is multi-valued).
        ids = list(target.values_list("client_id", flat=True).distinct())
        scoped = Client.objects.filter(client_id__in=ids)
        total = len(ids)

        self.stdout.write(head(f"\nTarget clients: {total}"))
        self.stdout.write("Current lifecycle_stage breakdown:")
        for r in (
            scoped.values("lifecycle_stage")
            .annotate(n=Count("client_id"))
            .order_by("-n")
        ):
            stage = r["lifecycle_stage"] or "(empty)"
            self.stdout.write(f"  {stage:>24}: {r['n']}")

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN: no changes written. Re-run with --apply to update."
                )
            )
            return

        with transaction.atomic():
            updated = scoped.update(
                lifecycle_stage=ClientStage.PENDING_VERIFICATION,
                lifecycle_stage_at=timezone.now(),
            )

        pending_total = Client.objects.filter(
            lifecycle_stage=ClientStage.PENDING_VERIFICATION
        ).count()
        self.stdout.write(self.style.SUCCESS(f"\nUpdated {updated} clients."))
        self.stdout.write(f"pending_verification total now: {pending_total}")
        self.stdout.write(
            self.style.WARNING(
                "Reminder: this is a derived field. A later recompute_client_stage "
                "will revert it unless the derivation/enrollment fix is applied."
            )
        )

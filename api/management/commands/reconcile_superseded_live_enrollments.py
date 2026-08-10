"""Close SUPERSEDED enrollments that were left non-terminal.

A governing-case replacement supersedes the old enrollment and should CLOSE it,
but a swallowed ``InvalidTransition`` previously left some superseded rows LIVE
(still at verified / kitchen_assignment). That produced TWO live enrollments for
one client -- so the profile / PO / nutritionist flows could read or act on the
stale one (e.g. a kitchen assigned on the real row while the profile showed the
stale one with no kitchen).

This closes each superseded-but-non-terminal enrollment, first carrying its
Nutritionist sign-off onto the surviving (superseding) enrollment so the approval
isn't lost, then recomputing the client's lifecycle stage.

Dry-run by default; pass --apply to commit.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Client, EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import recompute_client_stage

_TERMINAL = [EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED]

_NUTRI_FIELDS = [
    "nutritionist_approved_at", "nutritionist_approved_by",
    "nutritionist_signature", "nutritionist_signature_image",
    "nutritionist_approval_pdf_key",
]


class Command(BaseCommand):
    help = (
        "Close superseded-but-live enrollments (carrying nutritionist approval to "
        "the survivor). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")
        parser.add_argument(
            "--list", action="store_true",
            help="Print each affected enrollment (read-only detail).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        list_detail = opts["list"]

        # Superseded (something supersedes them) yet NOT terminal -> stale duplicates.
        qs = (
            EnrollmentVerification.objects
            .filter(superseded_by__isnull=False)
            .exclude(stage__in=[s.value for s in _TERMINAL])
            .select_related("client")
            .distinct()
        )
        total = qs.count()
        by_stage = Counter(qs.values_list("stage", flat=True))

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Close superseded-but-live enrollments ==="
        ))
        self.stdout.write(f"  stale duplicates to close: {total}")
        for stage, n in sorted(by_stage.items()):
            self.stdout.write(f"     {n:6}  {stage}")
        if list_detail:
            for e in qs.order_by("client__last_name", "client__first_name"):
                c = e.client
                name = (f"{c.first_name} {c.last_name}".strip() if c else "") or "?"
                self.stdout.write(
                    f"    enr={e.pk} client={e.client_id} {name:24.24} "
                    f"stage={e.stage:20.20} nutri={'Y' if e.nutritionist_approved_at else '-'}"
                )

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: nothing changed. Re-run with --apply."
            ))
            return

        closed = 0
        client_ids = set()
        now = timezone.now()
        ids = list(qs.values_list("pk", flat=True))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            with transaction.atomic():
                for old in EnrollmentVerification.objects.filter(pk__in=chunk):
                    survivor = old.superseded_by.first()
                    # Carry the sign-off to the survivor if it lacks its own.
                    if (
                        survivor is not None
                        and old.nutritionist_approved_at
                        and not survivor.nutritionist_approved_at
                    ):
                        for f in _NUTRI_FIELDS:
                            setattr(survivor, f, getattr(old, f))
                        survivor.save(update_fields=_NUTRI_FIELDS)
                    old.stage = EnrollmentStage.CLOSED
                    old.stage_at = now
                    old.close_reason = old.close_reason or "duplicate_superseded"
                    old.save(update_fields=["stage", "stage_at", "close_reason"])
                    closed += 1
                    if old.client_id:
                        client_ids.add(old.client_id)

        healed = 0
        for cid in client_ids:
            c = Client.objects.filter(pk=cid).first()
            if c is None:
                continue
            try:
                recompute_client_stage(c)
                healed += 1
            except Exception:  # noqa: BLE001
                self.stderr.write(f"  recompute failed for client {cid}")

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: closed {closed} stale enrollment(s); recomputed {healed} client(s)."
        ))

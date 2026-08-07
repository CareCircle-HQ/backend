"""Revert enrollments advanced PAST verification without the verification fact.

A governing-case replacement bug could force an enrollment forward to VERIFIED /
KITCHEN_ASSIGNMENT even though the household was never verified (``verified_at``
is NULL). Those rows read "Kitchen Assignment" on the profile but "Pending
Verification" in the list (which keys off the verification FACT), and never
belonged in the kitchen-assignment queue.

This reverts such rows to PENDING_VERIFICATION and recomputes the client's
lifecycle stage so the profile and the list agree again.

SERVING stages (SERVICE_ACTIVE / ON_HOLD / SERVICE_COMPLETE) with a NULL
``verified_at`` are only REPORTED, not auto-reverted, unless ``--include-serving``
is passed -- reverting a member mid-delivery is disruptive and warrants review.

Dry-run by default; pass --apply to commit.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Client, EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import recompute_client_stage

# Advanced PAST verification but PRE-service: safe to revert to Pending Verification.
_PRE_SERVICE_ADVANCED = [
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
]
# Serving stages: reverting is disruptive, so only with --include-serving.
_SERVING = [
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.ON_HOLD,
    EnrollmentStage.SERVICE_COMPLETE,
]


class Command(BaseCommand):
    help = (
        "Revert enrollments advanced past verification with a NULL verified_at "
        "back to Pending Verification. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")
        parser.add_argument(
            "--include-serving", action="store_true",
            help="Also revert SERVICE_ACTIVE / ON_HOLD / SERVICE_COMPLETE rows (disruptive).",
        )
        parser.add_argument(
            "--list", action="store_true",
            help="Print each affected member (client id / name / program / stage / "
                 "lifecycle) -- read-only diagnostic detail.",
        )

    def _print_rows(self, qs, label):
        rows = qs.select_related("client").order_by("client__last_name", "client__first_name")
        self.stdout.write(f"\n  --- {label} ---")
        for e in rows:
            c = e.client
            name = (f"{c.first_name} {c.last_name}".strip() if c else "") or "?"
            cid = str(e.client_id) if e.client_id else "-"
            lifecycle = getattr(c, "lifecycle_stage", "") if c else ""
            self.stdout.write(
                f"    {cid}  {name:24.24}  enr_stage={e.stage:20.20}  "
                f"lifecycle={lifecycle:20.20}  program={(e.program_name or '')[:30]}"
            )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        include_serving = opts["include_serving"]
        list_detail = opts["list"]

        target_stages = list(_PRE_SERVICE_ADVANCED)
        if include_serving:
            target_stages += _SERVING

        qs = EnrollmentVerification.objects.filter(
            verified_at__isnull=True,
            stage__in=[s.value for s in target_stages],
        )
        serving_qs = EnrollmentVerification.objects.filter(
            verified_at__isnull=True,
            stage__in=[s.value for s in _SERVING],
        )

        total = qs.count()
        by_stage = Counter(qs.values_list("stage", flat=True))

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Revert unverified enrollments advanced past verification ==="
        ))
        self.stdout.write(f"  to revert -> Pending Verification: {total}")
        for stage, n in sorted(by_stage.items()):
            self.stdout.write(f"     {n:6}  {stage}")
        if list_detail and total:
            self._print_rows(qs, "to revert")

        if not include_serving:
            serving_by_stage = Counter(serving_qs.values_list("stage", flat=True))
            n_serving = sum(serving_by_stage.values())
            if n_serving:
                self.stdout.write(self.style.WARNING(
                    f"  NOT touched (serving with NULL verified_at) -- review manually "
                    f"or re-run with --include-serving: {n_serving}"
                ))
                for stage, n in sorted(serving_by_stage.items()):
                    self.stdout.write(f"     {n:6}  {stage}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: nothing changed. Re-run with --apply."
            ))
            return

        reverted = 0
        client_ids = set()
        ids = list(qs.values_list("pk", flat=True))
        now = timezone.now()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            with transaction.atomic():
                for enr in EnrollmentVerification.objects.filter(pk__in=chunk):
                    enr.stage = EnrollmentStage.PENDING_VERIFICATION
                    enr.stage_at = now
                    enr.save(update_fields=["stage", "stage_at"])
                    reverted += 1
                    if enr.client_id:
                        client_ids.add(enr.client_id)

        # Recompute each affected client's lifecycle stage so profile + list agree.
        healed = 0
        for cid in client_ids:
            c = Client.objects.filter(pk=cid).first()
            if c is None:
                continue
            try:
                recompute_client_stage(c)
                healed += 1
            except Exception:  # noqa: BLE001 - never let one client abort the run
                self.stderr.write(f"  recompute failed for client {cid}")

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: reverted {reverted} enrollment(s) to Pending Verification; "
            f"recomputed {healed} client stage(s)."
        ))

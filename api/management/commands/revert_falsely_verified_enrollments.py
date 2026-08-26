"""Revert enrollments FALSELY marked verified by the case-replacement carry.

When the import replaces a governing case, ``_carry_service_and_activate`` carries
the prior enrollment's verification forward and ladders the new enrollment
``pending_verification -> verified -> kitchen_assignment``. The carry only checks
that ``verified_at`` is NON-NULL -- not that the verification was REAL -- so a
member whose "verification" is only a system stamp (``verified_by`` NULL, never a
real wizard verification, ``nutritionist_approved_at`` NULL) gets propagated into
Kitchen Assignment on every case replacement, skipping the verification wizard AND
the nutritionist step. A large batch of case replacements in one import (e.g.
2026-08-21) mass-advances these never-really-verified members.

This reverts such rows to PENDING_VERIFICATION: clears the false verification
fact, drops the carried kitchen/cadence, pulls future deliveries off the calendar,
and recomputes the client's lifecycle stage.

SIGNATURE (all required, so a REAL or grandfathered-nutritionist verification is
never clobbered):
  * a StageEvent with metadata trigger == "case_replaced"
  * ``verified_by`` IS NULL        (no real verifier -- carry copies verified_by,
                                    so NULL here means the source was never real)
  * ``nutritionist_approved_at`` IS NULL   (nutritionist step never happened)

SERVING rows (SERVICE_ACTIVE / ON_HOLD / SERVICE_COMPLETE) are only REPORTED, not
reverted, unless ``--include-serving``. Dry-run by default; ``--apply`` commits.
``--since YYYY-MM-DD`` limits to rows verified on/after that date (Friday's batch).
"""
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Client, EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import recompute_client_stage

_TRIGGER = "case_replaced"
_PRE_SERVICE = [EnrollmentStage.VERIFIED, EnrollmentStage.KITCHEN_ASSIGNMENT]
_SERVING = [
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.ON_HOLD,
    EnrollmentStage.SERVICE_COMPLETE,
]


class Command(BaseCommand):
    help = (
        "Revert case-replacement-carried, never-really-verified enrollments back "
        "to Pending Verification. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")
        parser.add_argument(
            "--include-serving", action="store_true",
            help="Also revert SERVICE_ACTIVE / ON_HOLD / SERVICE_COMPLETE rows (disruptive).",
        )
        parser.add_argument(
            "--since", default="",
            help="Only rows with verified_at on/after this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--list", action="store_true", help="Print each affected member.",
        )

    def _base_qs(self, since):
        qs = EnrollmentVerification.objects.filter(
            stage_events__metadata__trigger=_TRIGGER,
            verified_by__isnull=True,
            nutritionist_approved_at__isnull=True,
        ).distinct()
        if since:
            try:
                d = datetime.strptime(since, "%Y-%m-%d").date()
            except ValueError:
                raise SystemExit(f"--since must be YYYY-MM-DD, got {since!r}")
            qs = qs.filter(verified_at__date__gte=d)
        return qs

    def _print_rows(self, qs, label):
        self.stdout.write(f"\n  --- {label} ---")
        for e in qs.select_related("client").order_by("client__last_name", "client__first_name"):
            c = e.client
            name = ((f"{c.first_name} {c.last_name}".strip()) if c else "") or "?"
            cid = str(e.client_id) if e.client_id else "-"
            self.stdout.write(
                f"    {cid}  {name:24.24}  stage={e.stage:18.18}  "
                f"verified_at={str(e.verified_at)[:10]}  program={(e.program_name or '')[:26]}"
            )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        include_serving = opts["include_serving"]
        since = (opts["since"] or "").strip()

        all_sig = self._base_qs(since)
        revert_stages = list(_PRE_SERVICE) + (list(_SERVING) if include_serving else [])
        qs = all_sig.filter(stage__in=[s.value for s in revert_stages])
        serving_qs = all_sig.filter(stage__in=[s.value for s in _SERVING])

        total = qs.count()
        by_stage = Counter(qs.values_list("stage", flat=True))

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Revert falsely-verified (case_replaced carry) enrollments -> Pending Verification ==="
        ))
        self.stdout.write(f"  signature matches (verified_by=System, no nutritionist, case_replaced): {all_sig.count()}")
        if since:
            self.stdout.write(f"  (limited to verified_at >= {since})")
        self.stdout.write(f"  to revert: {total}")
        for stage, n in sorted(by_stage.items()):
            self.stdout.write(f"     {n:6}  {stage}")
        if opts["list"] and total:
            self._print_rows(qs, "to revert")

        if not include_serving:
            n_serving = serving_qs.count()
            if n_serving:
                self.stdout.write(self.style.WARNING(
                    f"  NOT touched (serving) -- review or re-run with --include-serving: {n_serving}"
                ))
                for stage, n in sorted(Counter(serving_qs.values_list("stage", flat=True)).items()):
                    self.stdout.write(f"     {n:6}  {stage}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing changed. Re-run with --apply."))
            return

        reverted, client_ids = 0, set()
        ids = list(qs.values_list("pk", flat=True))
        now = timezone.now()
        for i in range(0, len(ids), 500):
            with transaction.atomic():
                for enr in EnrollmentVerification.objects.filter(pk__in=ids[i:i + 500]):
                    try:
                        from api.services.orders import truncate_future_deliveries
                        truncate_future_deliveries(enr)
                    except Exception:  # noqa: BLE001
                        pass
                    enr.stage = EnrollmentStage.PENDING_VERIFICATION
                    enr.stage_at = now
                    enr.verified_at = None
                    enr.is_family_verified = False
                    enr.medicaid_type_verified = False
                    enr.delivery_address_verified = False
                    enr.kitchen = None
                    enr.delivery_weekdays = []
                    enr.closed_at = None
                    enr.save(update_fields=[
                        "stage", "stage_at", "verified_at", "is_family_verified",
                        "medicaid_type_verified", "delivery_address_verified",
                        "kitchen", "delivery_weekdays", "closed_at",
                    ])
                    reverted += 1
                    if enr.client_id:
                        client_ids.add(enr.client_id)

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
            f"\nAPPLIED: reverted {reverted} enrollment(s) to Pending Verification; "
            f"recomputed {healed} client stage(s)."
        ))

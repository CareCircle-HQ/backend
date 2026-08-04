"""Fix serving enrollments that lost their governing internal-service case.

Symptom (e.g. SHMUEL WECHSLER): a member's live SERVICE_ACTIVE/ON_HOLD enrollment
has ``case=None`` while the governing internal-service case sits on a SEPARATE,
usually pending_verification, enrollment -- a split left by a governing-case
replacement/re-verification. Because a per-case unique constraint forbids two
live enrollments sharing a case, the normal reconcile can't self-heal it (the
case is "taken" by the stray), so the serving enrollment stays caseless -- which
breaks its authorization window / PO handling and can cause delivery gaps.

Fix, per caseless serving enrollment:
  * SPLIT   -- the governing open ISC case is held by another NON-serving live
              enrollment (pending_verification/verified/kitchen_assignment):
              DISREGARD that stray (freeing the case), then point the serving
              enrollment at the case.
  * UNBOUND -- the governing open ISC case is on no live enrollment: just point
              the serving enrollment at it.
  * SKIP    -- no open ISC case, or the case is held by ANOTHER SERVING
              enrollment (two serving enrollments -> ambiguous, needs review).

Dry-run by default; pass --apply to commit. Optional --client to target one.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    CaseStatus, CaseType, EnrollmentStage, EnrollmentVerification,
)
from api.services.lifecycle import governing_case_key

TERMINAL = {EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED, EnrollmentStage.DISREGARDED}
SERVING = [EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.ON_HOLD]
CLOSED_CASE = [CaseStatus.CLOSED, CaseStatus.CANCELLED]


def _governing_open_isc(client):
    cases = [
        c for c in client.cases.all()
        if c.case_type == CaseType.INTERNAL_SERVICE and c.case_status not in CLOSED_CASE
    ]
    return max(cases, key=governing_case_key) if cases else None


class Command(BaseCommand):
    help = (
        "Bind the governing internal-service case back onto serving enrollments "
        "that lost it (freeing it from a stray enrollment first). Dry-run unless "
        "--apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the fixes.")
        parser.add_argument("--client", default="", help="Limit to one client_id.")

    def handle(self, *args, **options):
        apply = options["apply"]
        qs = (
            EnrollmentVerification.objects.filter(stage__in=SERVING, case__isnull=True)
            .select_related("client")
        )
        if options["client"]:
            qs = qs.filter(client_id=options["client"])

        buckets = Counter()
        plans = []  # (serving_enr, case, [stray_enrs_to_disregard])
        for enr in qs.iterator(chunk_size=500):
            c = enr.client
            if c is None:
                buckets["no_client"] += 1
                continue
            gov = _governing_open_isc(c)
            if gov is None:
                buckets["no_open_case"] += 1
                continue
            # The client must have EXACTLY ONE serving enrollment. Two serving
            # enrollments both wanting the one case is ambiguous -- and would make
            # two plans try to bind the same case, the second hitting the per-case
            # unique constraint. Skip for review (mirrors the reconcile helper).
            if EnrollmentVerification.objects.filter(client=c, stage__in=SERVING).count() != 1:
                buckets["ambiguous_two_serving"] += 1
                continue
            holders = list(
                EnrollmentVerification.objects.filter(client=c, case=gov)
                .exclude(pk=enr.pk)
                .exclude(stage__in=TERMINAL)
            )
            if any(h.stage in SERVING for h in holders):
                buckets["ambiguous_two_serving"] += 1
                continue
            buckets["split" if holders else "unbound"] += 1
            plans.append((enr, gov, holders))

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Caseless serving enrollments ==="))
        self.stdout.write(f"  fixable SPLIT (free stray + bind):   {buckets['split']}")
        self.stdout.write(f"  fixable UNBOUND (bind only):         {buckets['unbound']}")
        self.stdout.write(f"  SKIP two-serving (needs review):     {buckets['ambiguous_two_serving']}")
        self.stdout.write(f"  SKIP no open ISC case:               {buckets['no_open_case']}")
        self.stdout.write(f"  SKIP no client:                      {buckets['no_client']}")
        self.stdout.write(f"  -> total fixable: {len(plans)}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing changed. Re-run with --apply."))
            return

        fixed = strays = 0
        for i in range(0, len(plans), 200):
            for enr, gov, holders in plans[i:i + 200]:
                try:
                    with transaction.atomic():
                        for h in holders:
                            h.case = None
                            h.stage = EnrollmentStage.DISREGARDED
                            h.close_reason = "caseless_serving_fix"
                            h.save(update_fields=["case", "stage", "close_reason"])
                            strays += 1
                        enr.case = gov
                        enr.save(update_fields=["case"])
                        fixed += 1
                except Exception:  # noqa: BLE001 - one bad row can't stop the sweep
                    self.stdout.write(self.style.ERROR(f"  failed on enrollment {enr.pk}"))
        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: bound {fixed} serving enrollment(s) to their case; "
            f"disregarded {strays} stray enrollment(s)."
        ))

"""Backfill: reconcile enrollments stranded at an advanced stage with NO open
internal-service (meal/box) case.

Both ``pending_verification`` and ``kitchen_assignment`` presuppose an OPEN
internal-service case (the case verification attaches to / that carries the
authorization delivery runs under). When that case was closed or DELETED without
anything reconciling the enrollment, the household is left stranded at the
advanced stage. This one-off heals them, deriving the destination from the data
the client actually has:

* Bucket 1 -- has internal-service case(s) but NONE open (all closed/cancelled):
  the canonical closure full stop -> CANCELLED (routed through
  ``reconcile_internal_service_authorization`` so it also truncates future
  deliveries and notes the primary).
* Bucket 2 -- NO internal-service case at all (``pending_verification`` OR
  ``kitchen_assignment``) -> DISREGARDED. There is nothing to verify or deliver,
  so the enrollment is an orphan: it is dismissed (reversible, kept for history,
  removed from governance) and the client falls back to their derived early-funnel
  stage (Navigation / Eligible / ...).

Dry-run by default; pass --apply to commit. Idempotent -- re-running only acts on
rows that are still stranded.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import (
    _internal_service_cases,
    advance_enrollment,
    open_internal_service_cases,
    reconcile_internal_service_authorization,
)

_STAGES = [EnrollmentStage.PENDING_VERIFICATION, EnrollmentStage.KITCHEN_ASSIGNMENT]
_NOTE = "Orphan reconcile: no open internal-service (meal/box) case backing this enrollment."
_ACTOR_LABEL = "system:orphan-reconcile"


class Command(BaseCommand):
    help = (
        "Heal enrollments stuck at pending_verification / kitchen_assignment "
        "with no open internal-service case. Dry-run by default; --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply. Without this the command only previews.",
        )

    def _classify(self, enr):
        """Return the bucket for a stranded enrollment, or None if healthy."""
        client = enr.client
        if client is None:
            return None
        if open_internal_service_cases(client):
            return None  # healthy -- has an open internal case
        if _internal_service_cases(client):
            return "closed_case"  # bucket 1: closed case(s), none open
        return "no_case"  # bucket 2: no internal-service case at all

    def handle(self, *args, **opts):
        apply = opts["apply"]

        enrs = (
            EnrollmentVerification.objects.filter(stage__in=_STAGES)
            .select_related("client")
            .prefetch_related("client__cases")
        )

        buckets = {"closed_case": [], "no_case": []}
        for enr in enrs:
            bucket = self._classify(enr)
            if bucket:
                buckets[bucket].append(enr)

        counts = Counter({k: len(v) for k, v in buckets.items()})
        total = sum(counts.values())

        self.stdout.write("Stranded enrollments (no open internal-service case):")
        self.stdout.write(
            f"  Bucket 1  closed case(s), none open -> CANCELLED : {counts['closed_case']}"
        )
        self.stdout.write(
            f"  Bucket 2  no internal case at all   -> DISREGARDED: {counts['no_case']}"
        )
        for bucket, enr_list in buckets.items():
            for e in enr_list:
                self.stdout.write(
                    f"    [{bucket}] enrollment {e.pk} client {e.client_id} "
                    f"stage {e.stage}"
                )

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing stranded. No changes needed."))
            return

        if not apply:
            self.stdout.write(self.style.WARNING(
                f"\nDry run -- {total} enrollment(s) would be reconciled. "
                f"Re-run with --apply to commit."
            ))
            return

        healed = Counter()
        with transaction.atomic():
            # Bucket 1: route through the canonical closure handler, once per
            # client (it cancels every governing enrollment + truncates deliveries).
            for client in {e.client for e in buckets["closed_case"]}:
                reconcile_internal_service_authorization(
                    client, actor_label=_ACTOR_LABEL,
                )
                healed["closed_case"] += 1

            # Bucket 2: no internal-service case at all -> disregard the orphan
            # (both pending_verification and kitchen_assignment).
            for e in buckets["no_case"]:
                advance_enrollment(
                    e, EnrollmentStage.DISREGARDED,
                    actor_label=_ACTOR_LABEL, note=_NOTE,
                )
                healed["no_case"] += 1

        self.stdout.write(self.style.SUCCESS(
            f"Reconciled: {healed['closed_case']} client(s) via closure full-stop, "
            f"{healed['no_case']} orphan(s) disregarded."
        ))

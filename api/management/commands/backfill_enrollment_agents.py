"""Backfill EnrollmentVerification.verified_by / requested_by where they are NULL,
by copying the acting agent from the SAME CLIENT's most-recent enrollment that DOES
have one.

This only recovers the "propagation-drop" residue -- a client verified/requested by
a real agent on one enrollment, but a case switch / reauthorization / household
split dropped the agent on another before that path was fixed. It CANNOT touch the
bulk historical import roots (no agent exists on any of that client's enrollments),
and it never invents the agent running this command.

Rules:
  * verified_by is only filled on rows that are actually verified (verified_at set),
    so we never create a half-verified row.
  * requested_by can be filled on any row missing it.
  * source = the same client's most-recent enrollment that carries the field
    (verified_at desc for verified_by; requested_at/opened_at desc for requested_by).
  * writes via .update() -- no save() side effects, no timeline/history noise.

Dry-run by default; pass --apply to write.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentVerification


class Command(BaseCommand):
    help = (
        "Backfill EnrollmentVerification.verified_by/requested_by from the same "
        "client's most-recent attributed enrollment. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the changes (default is a dry run).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]

        # Per-client best (most-recent) agent for each field. Iterate ascending so
        # the LAST write (most recent) wins in the dict.
        verified_src = {}
        for row in (
            EnrollmentVerification.objects
            .filter(verified_by__isnull=False, client_id__isnull=False)
            .order_by("verified_at", "opened_at")
            .values("client_id", "verified_by_id")
        ):
            verified_src[row["client_id"]] = row["verified_by_id"]

        requested_src = {}
        for row in (
            EnrollmentVerification.objects
            .filter(requested_by__isnull=False, client_id__isnull=False)
            .order_by("requested_at", "opened_at")
            .values("client_id", "requested_by_id")
        ):
            requested_src[row["client_id"]] = row["requested_by_id"]

        # Targets missing the field, matched to a same-client source.
        verified_fix = []  # (pk, agent_id)
        for row in (
            EnrollmentVerification.objects
            .filter(verified_at__isnull=False, verified_by__isnull=True)
            .values("pk", "client_id")
        ):
            aid = verified_src.get(row["client_id"])
            if aid:
                verified_fix.append((row["pk"], aid))

        requested_fix = []
        for row in (
            EnrollmentVerification.objects
            .filter(requested_by__isnull=True)
            .values("pk", "client_id")
        ):
            aid = requested_src.get(row["client_id"])
            if aid:
                requested_fix.append((row["pk"], aid))

        self.stdout.write(
            f"verified_by  : {len(verified_fix)} row(s) recoverable from the same client"
        )
        self.stdout.write(
            f"requested_by : {len(requested_fix)} row(s) recoverable from the same client"
        )
        for label, fixes in (("verified_by", verified_fix), ("requested_by", requested_fix)):
            for pk, aid in fixes[:5]:
                self.stdout.write(f"    sample {label}: enr {pk} <- agent {aid}")

        if not apply:
            self.stdout.write("Dry run -- re-run with --apply to write.")
            return

        with transaction.atomic():
            for pk, aid in verified_fix:
                EnrollmentVerification.objects.filter(pk=pk).update(verified_by_id=aid)
            for pk, aid in requested_fix:
                EnrollmentVerification.objects.filter(pk=pk).update(requested_by_id=aid)

        self.stdout.write(self.style.SUCCESS(
            f"Applied: verified_by {len(verified_fix)}, requested_by {len(requested_fix)}."
        ))

"""Resolve the backlog of CASELESS "previous enrollments" -- superseded rows
(``close_reason='case_replaced'``) that were left with no case FK.

Every enrollment must reference its case; these legacy rows violate that. For
each caseless previous enrollment we look at the client's OTHER internal-service
cases (excluding the survivor's current case) and act per bucket:

  * 0 candidate prior cases -> FLAG ``hidden_misinformation`` (a pre-case
    placeholder with no distinct case to attach; hidden from the UI, purge later).
  * exactly 1 candidate      -> BACKFILL: bind that prior case onto the row and
    record it as the survivor's ``previous_case``.
  * 2+ candidates            -> AMBIGUOUS: printed for manual review, unchanged.

DRY-RUN by default (prints what WOULD change); pass ``--apply`` to commit. Use
``--client <id>`` to scope to one member. Idempotent.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, CaseType, EnrollmentVerification


class Command(BaseCommand):
    help = (
        "Flag caseless placeholder previous enrollments (no prior case), backfill "
        "the unambiguous ones, and list the ambiguous ones (dry-run; --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Commit the changes (default is a dry-run that changes nothing).",
        )
        parser.add_argument(
            "--client", default="",
            help="Only process this client_id (default: every affected client).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        only = (opts.get("client") or "").strip()

        superseded_ids = set(
            EnrollmentVerification.objects
            .filter(supersedes__isnull=False)
            .values_list("supersedes_id", flat=True)
        )
        prev = (
            EnrollmentVerification.objects
            .filter(pk__in=superseded_ids, case__isnull=True, close_reason="case_replaced")
            .select_related("client")
        )
        if only:
            prev = prev.filter(client__client_id=only)

        flagged = backfilled = 0
        ambiguous = []  # (client_id, enr_pk, [candidate case ids])

        for e in prev.iterator(chunk_size=500):
            survivor = EnrollmentVerification.objects.filter(supersedes=e).first()
            surv_case_id = survivor.case_id if survivor else None
            candidates = list(
                Case.objects
                .filter(client_id=e.client_id, case_type=CaseType.INTERNAL_SERVICE)
                .exclude(case_id=surv_case_id)
                .order_by("-case_created_at", "-date_opened")
            )

            if not candidates:
                flagged += 1
                if apply:
                    e.hidden_misinformation = True
                    e.save(update_fields=["hidden_misinformation"])
                continue

            if len(candidates) == 1:
                prior = candidates[0]
                backfilled += 1
                if apply:
                    with transaction.atomic():
                        e.case = prior
                        fields = ["case"]
                        if not e.program_name and prior.program_name:
                            e.program_name = prior.program_name
                            fields.append("program_name")
                        if not e.service_type and prior.service_type:
                            e.service_type = prior.service_type
                            fields.append("service_type")
                        e.save(update_fields=fields)
                        if survivor is not None and survivor.previous_case_id is None:
                            survivor.previous_case = prior
                            survivor.save(update_fields=["previous_case"])
                continue

            ambiguous.append(
                (str(e.client_id), e.pk, [str(c.case_id) for c in candidates])
            )

        if ambiguous:
            self.stdout.write("AMBIGUOUS (2+ candidate prior cases -- review manually):")
            self.stdout.write(f"  {'client_id':<38}{'enr':<8}candidate_case_ids")
            for cid, enr_pk, cases in ambiguous:
                self.stdout.write(f"  {cid:<38}{enr_pk:<8}{', '.join(cases)}")

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}: {flagged} flagged as misinformation (no prior case), "
            f"{backfilled} backfilled to their single prior case, "
            f"{len(ambiguous)} ambiguous (left for review)."
        ))

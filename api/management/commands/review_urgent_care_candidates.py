"""Review existing members and flag any Urgent Care ("Need Attention") candidates
whose ``is_new`` flag was missed.

A client is a candidate when they meet the SAME gate the import enforces
(``api.services.lifecycle.is_urgent_care_candidate``): an OPEN internal-service
(meal/box) case, NO verification requested yet, a VALID Medicaid insurance, and a
VALID social care coverage. Normally the CSV import / nightly Unite Us pull flags
these as they're ingested; this is the safety-net job that sweeps the whole
member base and flags anyone the import didn't (e.g. cases imported before this
rule existed, or whose coverage only became valid later).

SET-ONLY: it never clears ``is_new`` (that happens when a verification is
requested/completed). Dry-run by default (prints a breakdown of who WOULD be
flagged); pass --apply to actually set the flag.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Prefetch, Q

from api.models import (
    CaseStatus,
    CaseType,
    Client,
    EnrollmentVerification,
)
from api.services.lifecycle import (
    has_valid_medicaid,
    has_valid_social_care,
    is_urgent_care_candidate,
)


class Command(BaseCommand):
    help = (
        "Flag is_new=True for existing members who meet the Urgent Care gate "
        "(open internal-service case + no verification requested + valid "
        "Medicaid + valid social care) but were missed by the import. Dry-run "
        "by default; pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually set is_new. Without this the command only previews.",
        )
        parser.add_argument(
            "--limit", type=int, default=25,
            help="How many sample clients to print in the preview (default 25).",
        )

    def _candidate_queryset(self):
        """Clients that already hold an OPEN internal-service case and are NOT
        already flagged -- the only ones that can possibly become candidates.
        The full gate (coverage + no verification) is evaluated in Python over
        prefetched relations."""
        open_case = Q(
            cases__case_type=CaseType.INTERNAL_SERVICE,
        ) & ~Q(cases__case_status__in=(CaseStatus.CLOSED, CaseStatus.CANCELLED))
        return (
            Client.objects.filter(open_case, is_new=False)
            .distinct()
            .prefetch_related(
                "cases",
                "insurances",
                "social_care_coverages",
                "enrollments",
                Prefetch(
                    "household_membership__household__enrollment_verifications",
                    queryset=EnrollmentVerification.objects.all(),
                ),
            )
        )

    def handle(self, *args, **opts):
        qs = self._candidate_queryset()

        candidates = []
        # Reason counters for the members that DON'T qualify, so the preview
        # explains why the pool narrows (helps spot data issues).
        missing_medicaid = missing_social = has_verification = 0
        for client in qs:
            if is_urgent_care_candidate(client):
                candidates.append(client)
            elif not has_valid_medicaid(client):
                missing_medicaid += 1
            elif not has_valid_social_care(client):
                missing_social += 1
            else:
                # open case + coverage present but a verification already exists
                has_verification += 1

        n = len(candidates)
        self.stdout.write(
            f"{n} unflagged member(s) with an open internal-service case qualify "
            f"as Urgent Care candidates and would be flagged is_new."
        )
        self.stdout.write(
            "  Skipped (open case but not a candidate): "
            f"{missing_medicaid} no valid Medicaid, "
            f"{missing_social} no valid social care, "
            f"{has_verification} verification already requested."
        )

        if n == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to flag."))
            return

        for client in candidates[: opts["limit"]]:
            name = f"{client.first_name} {client.last_name}".strip()
            self.stdout.write(f"    {client.client_id}  {name}")
        extra = n - opts["limit"]
        if extra > 0:
            self.stdout.write(f"    ... and {extra} more")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to flag."
            ))
            return

        flagged = 0
        with transaction.atomic():
            for client in candidates:
                client.is_new = True
                client.save(update_fields=["is_new"])
                flagged += 1
        self.stdout.write(self.style.SUCCESS(
            f"Flagged {flagged} member(s) is_new=True."
        ))

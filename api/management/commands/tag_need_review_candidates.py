"""Find clients who hold all three program cases -- an OPEN internal-service
(meal/box) case, a Care Management (Navigation) case, and an Eligibility case --
but have NO verification enrollment yet (a verification was never requested for
them OR their household). These are members ready to be looked at, whether
they're a single individual or a member of a household.

Dry-run by default (prints who WOULD be tagged); pass --apply to attach the
"Need Review" client tag so they surface on Urgent Care -> Need Review.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Prefetch, Q

from api.models import (
    CaseStatus,
    CaseType,
    Client,
    ClientTag,
    EnrollmentVerification,
)
from api.services.lifecycle import (
    has_open_internal_service_case,
    has_verification_request,
)

NEED_REVIEW_TAG = "Need Review"


class Command(BaseCommand):
    help = (
        "Find clients with an OPEN internal-service case + a Care Management "
        "(Navigation) case + an Eligibility case and NO verification enrollment "
        "(own or household). Dry-run by default; pass --apply to add the "
        "\"Need Review\" tag."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually add the Need Review tag. Without this the command only previews.",
        )
        parser.add_argument(
            "--limit", type=int, default=50,
            help="How many sample clients to print in the preview (default 50).",
        )

    def _candidate_queryset(self):
        """Clients that already hold an OPEN internal-service case -- the only
        ones that can qualify. The rest of the gate (nav + eligibility case, no
        verification) is evaluated in Python over prefetched relations."""
        open_case = Q(
            cases__case_type=CaseType.INTERNAL_SERVICE,
        ) & ~Q(cases__case_status__in=(CaseStatus.CLOSED, CaseStatus.CANCELLED))
        return (
            Client.objects.filter(open_case)
            .distinct()
            .prefetch_related(
                "cases",
                "tags",
                "enrollments",
                Prefetch(
                    "household_membership__household__enrollment_verifications",
                    queryset=EnrollmentVerification.objects.all(),
                ),
            )
        )

    @staticmethod
    def _qualifies(client):
        if not has_open_internal_service_case(client):
            return False
        cases = list(client.cases.all())
        has_nav = any(c.case_type == CaseType.NAVIGATION for c in cases)
        has_elig = any(c.case_type == CaseType.ELIGIBILITY for c in cases)
        if not (has_nav and has_elig):
            return False
        # "No verification enrollment" -- neither their own nor their household's
        # (a verification was never requested for them).
        return not has_verification_request(client)

    def handle(self, *args, **opts):
        qs = self._candidate_queryset()

        candidates = []
        # Reason counters for members that DON'T qualify, so the preview explains
        # how the pool narrows.
        no_nav = no_elig = has_verification = 0
        for client in qs:
            if self._qualifies(client):
                candidates.append(client)
                continue
            cases = list(client.cases.all())
            if not any(c.case_type == CaseType.NAVIGATION for c in cases):
                no_nav += 1
            elif not any(c.case_type == CaseType.ELIGIBILITY for c in cases):
                no_elig += 1
            elif has_verification_request(client):
                has_verification += 1

        n = len(candidates)
        already_tagged = sum(
            1 for c in candidates
            if any(t.name == NEED_REVIEW_TAG for t in c.tags.all())
        )
        self.stdout.write(
            f"{n} client(s) have an open internal-service case + a Care Management "
            f"(Navigation) case + an Eligibility case and NO verification enrollment."
        )
        self.stdout.write(
            "  Skipped (open internal-service case but not a match): "
            f"{no_nav} no care-management case, "
            f"{no_elig} no eligibility case, "
            f"{has_verification} verification already requested."
        )
        if already_tagged:
            self.stdout.write(
                f"  ({already_tagged} of the {n} already carry the "
                f"\"{NEED_REVIEW_TAG}\" tag.)"
            )

        if n == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to tag."))
            return

        for client in candidates[: opts["limit"]]:
            name = f"{client.first_name or ''} {client.last_name or ''}".strip()
            membership = getattr(client, "household_membership", None)
            where = (
                ("primary of" if membership.is_primary else "member of")
                + " a household"
            ) if membership is not None else "individual"
            self.stdout.write(f"    {client.client_id}  {name}  [{where}]")
        extra = n - opts["limit"]
        if extra > 0:
            self.stdout.write(f"    ... and {extra} more")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to add the "
                f"\"{NEED_REVIEW_TAG}\" tag."
            ))
            return

        tag, _ = ClientTag.objects.get_or_create(name=NEED_REVIEW_TAG)
        tagged = 0
        with transaction.atomic():
            for client in candidates:
                if not any(t.name == NEED_REVIEW_TAG for t in client.tags.all()):
                    client.tags.add(tag)
                    tagged += 1
        self.stdout.write(self.style.SUCCESS(
            f"Added the \"{NEED_REVIEW_TAG}\" tag to {tagged} client(s)."
        ))

"""Flag clients who need a provider attestation, mirroring the ext.

On the ext (GHL/Unite Us), every eligibility population carries a
``"<Population> - Verification Method"`` question on the eligibility Assessment.
When its answer is ``"Provider Attestation"`` the member must supply provider
(doctor) attestation, and the ext sets the ``attestation_needed`` flag so the
screener completes the doctor information.

This command replays that rule over the imported eligibility ``Assessment``
records and sets :attr:`Client.attestation_needed` accordingly, so the portal's
Urgent Care -> "Need Attestation" tab (``attestation_needed=True``) is populated
even for members created before the flag was synced.

Additive by default: it only sets the flag TRUE for matching clients and never
clears it. Dry-run by default; pass ``--commit`` to persist.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Assessment, Client

# The verification-method answer that means "provider/doctor attestation
# required" (vs "ESMF" or "Member Attestation").
_PROVIDER_ATTESTATION = "provider attestation"
_VERIFICATION_METHOD_SUFFIX = "verification method"


class Command(BaseCommand):
    help = (
        "Set Client.attestation_needed=True for clients whose eligibility "
        "Assessment has a '… - Verification Method' answer of 'Provider "
        "Attestation'."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Persist changes (default is a dry run).",
        )

    @staticmethod
    def _needs_attestation(assessment):
        """True if any '… - Verification Method' answer is 'Provider Attestation'."""
        for qa in assessment.questions_answers or []:
            question = (qa.get("question") or "").strip().lower()
            answer = (qa.get("answer") or "").strip().lower()
            if question.endswith(_VERIFICATION_METHOD_SUFFIX) and (
                answer == _PROVIDER_ATTESTATION
            ):
                return True
        return False

    def handle(self, *args, **options):
        commit = options["commit"]

        # Client ids whose eligibility assessment requires provider attestation.
        client_ids = set()
        for a in (
            Assessment.objects.exclude(questions_answers=[]).iterator(chunk_size=500)
        ):
            if self._needs_attestation(a):
                cid = a.client_id or a.subject_id
                if cid:
                    client_ids.add(cid)

        matched = Client.objects.filter(client_id__in=client_ids)
        already = matched.filter(attestation_needed=True).count()
        to_set = matched.filter(attestation_needed=False)
        to_set_count = to_set.count()

        self.stdout.write(
            f"Assessments flag {len(client_ids)} distinct client(s) as needing "
            f"provider attestation."
        )
        self.stdout.write(
            f"  already flagged: {already} | to set True: {to_set_count} | "
            f"unresolved client ids: {len(client_ids) - matched.count()}"
        )

        if commit and to_set_count:
            with transaction.atomic():
                updated = to_set.update(attestation_needed=True)
            self.stdout.write(self.style.SUCCESS(
                f"Committed: {updated} client(s) set attestation_needed=True."
            ))
        elif to_set_count:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes written. Re-run with --commit to persist."
            ))
        else:
            self.stdout.write("Nothing to update.")

"""READ-ONLY report of clients stranded after their meal case closed: they have
NO live enrollment but DO have a NEW open, approved internal-service case and a
prior verified (terminal) enrollment to clone from -- i.e. exactly what
``reopen_enrollment_for_new_case`` will reopen on the next reconcile.

For each it shows the prior enrollment, the new open case, the no-open-case gap,
and whether the reopen will RESUME service (gap <= 60 days) or require
RE-VERIFICATION (gap > 60 days). Nothing is modified.

    python manage.py report_reopenable_enrollments
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import (
    CaseStatus,
    CaseType,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    ServiceAuthorizationStatus,
)
from api.services.lifecycle import (
    _LIVE_ENROLLMENT_STAGES,
    _REOPEN_REVERIFY_GAP_DAYS,
    _REOPEN_SOURCE_STAGES,
    pick_governing_case,
)


class Command(BaseCommand):
    help = "Read-only report of clients a new open case will reopen (JUAN's state)."

    def handle(self, *args, **opts):
        source_stage_values = [s.value for s in _REOPEN_SOURCE_STAGES]
        # set() dedupes reliably (values_list().distinct() can duplicate when the
        # model has Meta.ordering -- the order field joins the DISTINCT).
        client_ids = set(
            EnrollmentVerification.objects.filter(
                verified_at__isnull=False, stage__in=source_stage_values,
            ).values_list("client_id", flat=True)
        )

        resume = reverify = 0
        now = timezone.now()
        for cid in client_ids:
            enrs = list(EnrollmentVerification.objects.filter(client_id=cid))
            if any(EnrollmentStage(e.stage) in _LIVE_ENROLLMENT_STAGES for e in enrs):
                continue  # a live enrollment exists -> not stranded
            client = Client.objects.filter(pk=cid).first()
            if client is None:
                continue
            cases = [x for x in client.cases.all() if x.case_type == CaseType.INTERNAL_SERVICE]
            gov = pick_governing_case(cases)
            if gov is None:
                continue
            if gov.service_authorization_status not in (
                ServiceAuthorizationStatus.APPROVED, ServiceAuthorizationStatus.NOT_REQUIRED,
            ):
                continue
            if gov.case_status in (CaseStatus.CLOSED, CaseStatus.CANCELLED):
                continue
            priors = [
                e for e in enrs
                if e.verified_at and EnrollmentStage(e.stage) in _REOPEN_SOURCE_STAGES
            ]
            if not priors:
                continue
            prior = max(priors, key=lambda e: (e.closed_at or e.stage_at or e.opened_at))
            closed = prior.closed_at or prior.stage_at
            gap = (now - closed).days if closed else 0
            will_reverify = gap > _REOPEN_REVERIFY_GAP_DAYS
            if will_reverify:
                reverify += 1
            else:
                resume += 1
            self.stdout.write(
                f"  {client.client_id} {client.first_name} {client.last_name} | "
                f"prior enr {prior.pk} ({prior.stage}) closed {str(closed)[:10]} | "
                f"new case {str(gov.case_id)[:8]} ({(gov.program_name or '')[:28]}) | "
                f"gap {gap}d -> {'RE-VERIFY' if will_reverify else 'RESUME service'}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n{resume + reverify} client(s) will reopen on next reconcile: "
            f"{resume} resume service (<= {_REOPEN_REVERIFY_GAP_DAYS}d), "
            f"{reverify} require re-verification (> {_REOPEN_REVERIFY_GAP_DAYS}d)."
        ))

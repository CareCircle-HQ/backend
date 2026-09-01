"""Reopen members stranded on a CLOSED enrollment despite a new OPEN + APPROVED
internal-service case.

Root cause: when an enrollment's verification is reverted on an authorization
denial its ``verified_at`` is cleared, then the enrollment closes. When a new
open+approved case later arrives, ``reopen_enrollment_for_new_case`` used to skip
them (it required a verified prior) -- so they sat on a closed enrollment with a
good case and never resumed. The reopen now falls back to a data-complete prior;
this command remediates the members already stranded before that fix shipped.

Landing stage (decided by the shared reopen helper):
  * nutrition approved            -> Kitchen Assignment
  * verified (complete intake)    -> Verified / Pending Nutritionist
  * incomplete intake             -> Pending Verification

    python manage.py reopen_stranded_approved_enrollments            # dry run
    python manage.py reopen_stranded_approved_enrollments --apply
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Reopen enrollments stranded closed under a new open+approved case."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Persist the reopens (default: dry run, rolled back).",
        )

    def handle(self, *args, **opts):
        from django.db import transaction

        from api.models import (
            Case, CaseType, Client, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from api.portal import serializers as s
        from api.services.lifecycle import (
            _LIVE_ENROLLMENT_STAGES, _REOPEN_SOURCE_STAGES,
            reopen_enrollment_for_new_case,
        )

        apply = opts["apply"]
        open_appr = Case.objects.filter(
            case_type=CaseType.INTERNAL_SERVICE, case_status="open",
            service_authorization_status__in=[
                ServiceAuthorizationStatus.APPROVED,
                ServiceAuthorizationStatus.NOT_REQUIRED,
            ],
        )
        cids = list(open_appr.values_list("client_id", flat=True).distinct())
        qs = Client.objects.filter(client_id__in=cids).prefetch_related(
            "enrollments", "enrollments__member_profiles", "cases",
        )

        reopened, skipped, errors = 0, 0, 0
        by_stage = {}
        for c in qs:
            enrs = list(c.enrollments.all())
            # A live (funnel/serving) enrollment means the normal path owns it.
            if any(EnrollmentStage(e.stage) in _LIVE_ENROLLMENT_STAGES for e in enrs):
                continue
            # Need a terminal enrollment carrying a roster to clone from.
            if not any(
                EnrollmentStage(e.stage) in _REOPEN_SOURCE_STAGES
                and e.member_profiles.exists()
                for e in enrs
            ):
                continue
            gov = s.internal_service_case(c) or next(
                (ca for ca in c.cases.all() if ca.case_status == "open"), None
            )
            if gov is None:
                continue
            try:
                with transaction.atomic():
                    new = reopen_enrollment_for_new_case(
                        c, gov, actor_label="Stranded-approved reopen remediation",
                    )
                    if new is None:
                        skipped += 1
                        transaction.set_rollback(True)
                        continue
                    by_stage[new.stage] = by_stage.get(new.stage, 0) + 1
                    reopened += 1
                    self.stdout.write(
                        f"  {'REOPENED' if apply else 'would reopen'} "
                        f"{c.client_id} -> enr {new.pk} [{new.stage}]"
                    )
                    if not apply:
                        transaction.set_rollback(True)
            except Exception as exc:  # noqa: BLE001 - isolate one bad client
                errors += 1
                self.stderr.write(f"  ERROR {c.client_id}: {exc}")

        self.stdout.write("")
        self.stdout.write(
            ("APPLIED: " if apply else "DRY RUN (no changes): ")
            + f"reopened {reopened}, skipped {skipped}, errors {errors}."
        )
        if by_stage:
            self.stdout.write("  by landing stage: " + ", ".join(
                f"{k}={v}" for k, v in sorted(by_stage.items())
            ))
        if not apply:
            self.stdout.write("Re-run with --apply to persist.")

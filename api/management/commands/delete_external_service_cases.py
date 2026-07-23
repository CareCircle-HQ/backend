"""Delete every Case classified as External Service.

Case type is auto-derived on save (program_name -> ActiveProgram category,
falling back to the service_type heuristic), so this targets
``Case.case_type == CaseType.EXTERNAL_SERVICE``.

Deleting a case cascades to its ``ContractedService`` rows; any
``EnrollmentVerification`` or ``previous_case`` pointer is SET_NULL (those rows
are kept, just unlinked). Internal Service / Navigation / Eligibility cases are
never touched.

Safety: prints a summary and requires confirmation. Pass ``--yes`` to skip the
prompt (e.g. in a script) or ``--dry-run`` to only report what WOULD be deleted.

Usage:
    python manage.py delete_external_service_cases --dry-run
    python manage.py delete_external_service_cases            # prompts y/N
    python manage.py delete_external_service_cases --yes      # no prompt
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, CaseType, ContractedService


class Command(BaseCommand):
    help = "Delete all cases whose case type is External Service."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Report what would be deleted without deleting anything.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            dest="assume_yes",
            help="Skip the confirmation prompt (for non-interactive scripts).",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        assume_yes = options.get("assume_yes", False)

        qs = Case.objects.filter(case_type=CaseType.EXTERNAL_SERVICE)
        case_count = qs.count()
        cs_count = ContractedService.objects.filter(
            case__case_type=CaseType.EXTERNAL_SERVICE
        ).count()

        if case_count == 0:
            self.stdout.write(self.style.SUCCESS("No External Service cases found. Nothing to do."))
            return

        self.stdout.write(
            f"Found {case_count} External Service case(s), "
            f"{cs_count} linked contracted service(s) will cascade-delete."
        )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN: nothing deleted. Re-run without --dry-run to apply."
            ))
            return

        if not assume_yes:
            confirm = input(
                f"Delete {case_count} External Service case(s)? This cannot be "
                f"undone. Type 'yes' to continue: "
            ).strip().lower()
            if confirm != "yes":
                self.stdout.write(self.style.WARNING("Aborted. Nothing deleted."))
                return

        with transaction.atomic():
            deleted, by_model = qs.delete()

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {by_model.get('api.Case', 0)} External Service case(s) "
            f"({deleted} row(s) total across cascaded relations)."
        ))

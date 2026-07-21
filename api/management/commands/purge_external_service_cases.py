"""Delete stored External Service cases -- they are out of scope and should
never have been persisted.

External Service cases (Furniture/Home Goods, Food Pantry, Nutrition Education,
Housing Case Management, etc.) are services provided by OTHER programs, not our
internal meal/box service. The importers now skip them and ``CaseSerializer``
rejects them (see ``api.serializers.derive_case_type`` +
the ``EXTERNAL_SERVICE`` guard), but legacy rows imported before those programs
were classified as "External Services" in ``ProgramPipeline`` remain in the DB
and surface on member Case tabs.

Deleting a Case cascades ONLY to its ``ContractedService`` rows (the external
service deliveries, which are equally out of scope). Every other relation is
``SET_NULL`` -- ``EnrollmentVerification``, ``Note``, ``Ticket``,
``TimelineEvent`` and the ``Case.previous_case`` self-link stay put and simply
lose the (now-deleted) case reference.

Because the classification is authoritative, this re-derives each case's type at
purge time and only deletes rows that STILL resolve to External Service, so a
mis-seeded ProgramPipeline row can't cause an internal/eligibility/navigation
case to be dropped.

Dry-run unless ``--apply`` so you can review the counts first.

Usage:
    python manage.py purge_external_service_cases
    python manage.py purge_external_service_cases --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, CaseType, ContractedService
from api.serializers import derive_case_type


class Command(BaseCommand):
    help = (
        "Delete stored External Service cases (out of scope). Re-derives each "
        "case's type so only genuine External Service rows are removed. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit deletions.")

    def handle(self, *args, **options):
        apply = options["apply"]

        stored = Case.objects.filter(case_type=CaseType.EXTERNAL_SERVICE)
        # Re-derive to be safe: only purge rows that STILL classify as external.
        to_delete_ids = []
        reclassified = Counter()
        for case_id, service_type, program_name in stored.values_list(
            "case_id", "service_type", "program_name"
        ):
            derived = derive_case_type(service_type, program_name)
            if derived == CaseType.EXTERNAL_SERVICE:
                to_delete_ids.append(case_id)
            else:
                # No longer classifies as external (e.g. pipeline changed) --
                # leave it and report so it can be re-derived separately.
                reclassified[derived or "unclassified"] += 1

        clients = (
            Case.objects.filter(case_id__in=to_delete_ids)
            .values("client_id")
            .distinct()
            .count()
        )
        cs_count = ContractedService.objects.filter(
            case_id__in=to_delete_ids
        ).count()

        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Purge External Service cases ==="))
        self.stdout.write(f"  {'stored external_service cases':<38}: {stored.count()}")
        self.stdout.write(f"  {'confirmed external (to delete)':<38}: {len(to_delete_ids)}")
        self.stdout.write(f"  {'distinct clients affected':<38}: {clients}")
        self.stdout.write(f"  {'contracted services (cascade delete)':<38}: {cs_count}")
        if reclassified:
            self.stdout.write(
                head("\n  no longer external (kept -- re-derive separately):")
            )
            for k, v in reclassified.items():
                self.stdout.write(f"    {str(k):<20}: {v}")

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN: nothing deleted. Re-run with --apply to commit."
                )
            )
            return

        with transaction.atomic():
            deleted, by_model = Case.objects.filter(
                case_id__in=to_delete_ids
            ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"\nAPPLIED: deleted {by_model.get('api.Case', 0)} case(s) "
                f"(+{by_model.get('api.ContractedService', 0)} contracted services). "
                f"Remaining external_service cases: "
                f"{Case.objects.filter(case_type=CaseType.EXTERNAL_SERVICE).count()}."
            )
        )

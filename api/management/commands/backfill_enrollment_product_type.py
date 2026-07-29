"""Autofill the product kind (Meals / Boxes) each existing enrollment was
VERIFIED under onto ``EnrollmentVerification.product_type_override``.

Mirrors the household-scope backfill: the enrollment then carries its own
verified product kind, so a LATER divergence from the governing case (driven by
the case/program name) surfaces as a mismatch the Household tab lets an agent
reconcile. The governing case is never changed.

Only enrollments WITHOUT an override are touched; the verified kind is resolved
from the governing/tied case via the canonical resolver. Dry-run by default;
pass ``--commit`` to persist.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentVerification, ProductType
from api.services.catalog import (
    detected_product_kind_for_enrollment,
    product_kind_for_enrollment,
)


class Command(BaseCommand):
    help = (
        "Stamp each override-less enrollment's verified product kind "
        "(Meals/Boxes) onto product_type_override from its governing case."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Persist changes (default is a dry run).",
        )

    def handle(self, *args, **options):
        commit = options["commit"]

        # Cache ProductType rows by kind so we don't query per enrollment.
        pt_by_kind = {pt.type: pt for pt in ProductType.objects.all()}

        qs = EnrollmentVerification.objects.filter(
            product_type_override__isnull=True
        ).select_related("case")
        scanned = 0
        to_set = []  # (pk, kind)
        for enr in qs.iterator():
            scanned += 1
            kind = (
                detected_product_kind_for_enrollment(enr)
                or product_kind_for_enrollment(enr)
            )
            if kind is not None and kind in pt_by_kind:
                to_set.append((enr.pk, kind))

        self.stdout.write(f"Scanned {scanned} override-less enrollment(s).")
        self.stdout.write(f"  resolvable product kind: {len(to_set)}")

        if not to_set:
            self.stdout.write("Nothing to update.")
            return
        if not commit:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes written. Re-run with --commit to persist."
            ))
            return

        updated = 0
        with transaction.atomic():
            for pk, kind in to_set:
                updated += EnrollmentVerification.objects.filter(pk=pk).update(
                    product_type_override=pt_by_kind[kind]
                )
        self.stdout.write(self.style.SUCCESS(f"Committed: {updated} enrollment(s)."))

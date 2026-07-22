"""Recompute every case's ``household_type`` from its program name.

The Individual/Household classification is derived by
:func:`api.serializers.derive_household_type`, which was changed to key ONLY on
the word "Household" in the program name (a Met Council "(Household)" eligibility
pathway) and to ignore the client's household data (``is_a_family`` /
``household_size``). That client-data condition previously flipped single-member
cases to Household and was a frequent source of misclassification.

This command replays the current rule over ALL existing cases so the stored
field (used by the dashboard meals/boxes breakdown and admin filters) matches
the rule. New/edited cases are already classified on save via
``CaseSerializer``; this only fixes historical rows.

Dry-run by default; pass ``--commit`` to persist.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case
from api.serializers import derive_household_type


class Command(BaseCommand):
    help = (
        "Recompute Case.household_type from the program name (Household iff the "
        "word 'Household' appears in the program name)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Persist changes (default is a dry run).",
        )

    def handle(self, *args, **options):
        commit = options["commit"]

        total = 0
        to_change = []  # (pk, old, new)
        for case in (
            Case.objects.all()
            .only("pk", "household_type", "program_name")
            .iterator(chunk_size=1000)
        ):
            total += 1
            new = derive_household_type(None, case.program_name)
            if case.household_type != new:
                to_change.append((case.pk, case.household_type, new))

        self.stdout.write(f"Scanned {total} case(s).")
        self.stdout.write(f"  reclassification needed: {len(to_change)}")
        for pk, old, new in to_change[:20]:
            self.stdout.write(f"    {pk}: {old} -> {new}")
        if len(to_change) > 20:
            self.stdout.write(f"    ... and {len(to_change) - 20} more")

        if not to_change:
            self.stdout.write("Nothing to update.")
            return

        if not commit:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes written. Re-run with --commit to persist."
            ))
            return

        updated = 0
        with transaction.atomic():
            for pk, _old, new in to_change:
                updated += Case.objects.filter(pk=pk).update(household_type=new)
        self.stdout.write(self.style.SUCCESS(
            f"Committed: {updated} case(s) reclassified."
        ))

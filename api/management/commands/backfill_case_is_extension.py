"""Backfill ``Case.is_extension`` from the ActiveProgram ``to_extend`` flag.

A case is an EXTENSION / reauthorization when its ``program_name`` matches an
``ActiveProgram`` row flagged ``to_extend`` (see Settings > Programs, seeded True
for internal-service ``Reauthorization: ...`` programs). New cases derive this on
upsert (``api.serializers.derive_is_extension``); this command reconciles EXISTING
cases -- e.g. after an admin toggles ``to_extend`` on a program, or as a one-time
backfill.

Uses ``.update()`` (no history signals) so historical rows aren't churned. It
both SETS ``is_extension=True`` on cases now matching a to_extend program and
CLEARS it on cases that no longer match, so re-running always converges.

Dry-run by default.

Usage:
    python manage.py backfill_case_is_extension            # dry run
    python manage.py backfill_case_is_extension --apply
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from api.models import ActiveProgram, Case


class Command(BaseCommand):
    help = "Reconcile Case.is_extension from ActiveProgram.to_extend. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")

    def handle(self, *args, **opts):
        apply = opts["apply"]

        names = list(
            ActiveProgram.objects.filter(to_extend=True).values_list(
                "program_name", flat=True
            )
        )
        match = Q()
        for n in names:
            match |= Q(program_name__iexact=n)

        # Cases that SHOULD be flagged but aren't, and cases flagged that no
        # longer match (so a cleared to_extend flag also converges).
        to_set = Case.objects.filter(match, is_extension=False) if names else Case.objects.none()
        to_clear = (
            Case.objects.filter(is_extension=True).exclude(match)
            if names else Case.objects.filter(is_extension=True)
        )

        n_set = to_set.count()
        n_clear = to_clear.count()

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Backfill Case.is_extension from ActiveProgram.to_extend ==="
        ))
        self.stdout.write(f"  to_extend programs: {len(names)}")
        self.stdout.write(f"  cases to SET   is_extension=True : {n_set}")
        self.stdout.write(f"  cases to CLEAR is_extension=False: {n_clear}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run -- re-run with --apply."
            ))
            return

        set_done = to_set.update(is_extension=True) if names else 0
        clear_done = to_clear.update(is_extension=False)
        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: set {set_done}; cleared {clear_done}."
        ))

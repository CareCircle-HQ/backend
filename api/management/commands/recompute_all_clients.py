"""Recompute every client's lifecycle_stage from current data.

A client's ``lifecycle_stage`` is *derived* from synced cases/enrollments by
``recompute_client_stage``. When cases are imported or reclassified without a
subsequent recompute, the stored value goes stale (e.g. an approved
internal-service case but the client still reads ``inactive``). This walks all
clients, compares stored vs derived, and (with --apply) writes the fix.

    python manage.py recompute_all_clients            # dry run: list stale clients
    python manage.py recompute_all_clients --apply    # write the derived stage
    python manage.py recompute_all_clients --limit 20 # cap printed detail lines

Dry-run by default; no writes happen without --apply.
"""

from django.core.management.base import BaseCommand

from api.models import Client, ClientStage
from api.services.lifecycle import derive_client_stage, recompute_client_stage

# Funnel ordering (low -> high) so we can classify a recompute as an upgrade,
# downgrade, or lateral move. not_eligible is a terminal off-ramp, ranked high.
_FUNNEL_ORDER = [
    ClientStage.INACTIVE,
    ClientStage.CONSENT,
    ClientStage.SCREENED,
    ClientStage.ASSESSMENT,
    ClientStage.NAVIGATION,
    ClientStage.PENDING_VERIFICATION,
    ClientStage.VERIFIED,
    ClientStage.WAITING_AUTHORIZATION,
    ClientStage.AUTHORIZED,
    ClientStage.KITCHEN_ASSIGNMENT,
    ClientStage.ACTIVE,
    ClientStage.COMPLETED,
    ClientStage.NOT_ELIGIBLE,
]
_RANK = {str(s): i for i, s in enumerate(_FUNNEL_ORDER)}


def _direction(stored, derived):
    a, b = _RANK.get(str(stored), -1), _RANK.get(str(derived), -1)
    if b > a:
        return "upgrade"
    if b < a:
        return "downgrade"
    return "lateral"


class Command(BaseCommand):
    help = "Recompute lifecycle_stage for all clients (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Persist the derived stage. Without this it is a dry run.",
        )
        parser.add_argument(
            "--limit", type=int, default=50,
            help="Max number of stale clients to print in detail (default 50).",
        )
        parser.add_argument(
            "--only", choices=["upgrade", "downgrade", "lateral"], default=None,
            help="Restrict the scan/apply to one direction. Use --only upgrade "
                 "to safely advance clients without ever moving any backward.",
        )
        parser.add_argument(
            "--show", choices=["upgrade", "downgrade", "lateral"], default=None,
            help="Only print detail lines for this direction (does not affect "
                 "what is applied; use --only for that).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        limit = options["limit"]
        only = options["only"]
        show = options["show"]

        clients = Client.objects.all()
        total = clients.count()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Scanning {total} clients ({'APPLY' if apply else 'dry run'}"
            + (f", only={only}" if only else "") + ")"
        ))

        stale = []
        for client in clients.iterator():
            stored = client.lifecycle_stage
            derived = derive_client_stage(client)
            if str(stored) == str(derived):
                continue
            direction = _direction(stored, derived)
            if only and direction != only:
                continue
            stale.append((client, stored, derived, direction))

        self.stdout.write(
            f"\nStale clients (stored != derived): {len(stale)} / {total}"
        )

        # Direction breakdown FIRST: downgrades are the risky ones to review.
        directions = {"upgrade": 0, "downgrade": 0, "lateral": 0}
        for _, _, _, direction in stale:
            directions[direction] += 1
        self.stdout.write(
            "  upgrades: {upgrade}  downgrades: {downgrade}  lateral: {lateral}"
            .format(**directions)
        )
        if directions["downgrade"]:
            self.stdout.write(self.style.WARNING(
                "  Downgrades move a client BACKWARD - review these before --apply "
                "(usually a case the derivation can't see, e.g. provider mismatch)."
            ))

        printed = [s for s in stale if not show or s[3] == show]
        for client, stored, derived, direction in printed[:limit]:
            self.stdout.write(
                f"  - [{direction:<9}] {client.pk}  "
                f"{client.first_name} {client.last_name}: {stored} -> {derived}"
            )
        if len(printed) > limit:
            self.stdout.write(f"  ... and {len(printed) - limit} more")

        # Per-derived-stage breakdown so we can sanity-check the spread.
        breakdown = {}
        for _, _, derived, _ in stale:
            breakdown[str(derived)] = breakdown.get(str(derived), 0) + 1
        if breakdown:
            self.stdout.write("\nWould transition TO:")
            for stage, count in sorted(breakdown.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {stage:<22} {count}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run. Re-run with --apply to persist these derived stages."
            ))
            return

        changed = 0
        for client, _, _, _ in stale:
            recompute_client_stage(client, save=True)
            changed += 1
        self.stdout.write(self.style.SUCCESS(
            f"\nApplied. Recomputed {changed} clients."
        ))

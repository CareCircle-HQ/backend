"""Probe Unite Us for members whose person id was migrated to a NEW canonical id
-- without the browser extension.

For every client with an OPEN internal-service (governing) case that we have NOT
already recorded as migrated, we call ``GET /people/<id>``. Unite Us answers a
migrated id with a 301 -> the new person, which ``requests`` follows, so the
returned body's ``data.id`` is the new id. When it differs from the requested id
the person was migrated: we print ``old -> new`` plus whether the two records
pass the strict identity gate (DOB + first + last + Medicaid id) that would make
them safe to auto-merge.

READ-ONLY: probes Unite Us and prints. Makes NO local changes and merges nothing
-- the point is an accurate list of already-migrated members. Uses the stored
credential in read-only mode (no refresh-token rotation).

    python manage.py detect_uniteus_migrations               # all open-case members
    python manage.py detect_uniteus_migrations --limit 50     # smoke test
    python manage.py detect_uniteus_migrations --client-id <uuid> [--client-id ...]
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef, Q

from api.models import Case, CaseStatus, CaseType, Client


class Command(BaseCommand):
    help = (
        "Probe Unite Us for members whose person id was migrated to a new id "
        "(read-only; prints old -> new)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Only probe the first N clients.")
        parser.add_argument("--provider-id", type=str, default=None,
                            help="Use the credential for this provider.")
        parser.add_argument("--client-id", action="append", default=None,
                            help="Probe only the given client id(s); repeatable.")

    def handle(self, *args, **opts):
        from api.integrations.uniteus import api as uu_api
        from api.integrations.uniteus.api import UniteUsApiError, UniteUsAuthExpired
        from api.services.client_migration import (
            detect_api_migration, identity_matches_for_merge, resolve_client,
        )
        from api.services.uniteus_import import _select_active_credential

        cred = _select_active_credential(provider_id=opts["provider_id"])
        if cred is None:
            self.stderr.write(self.style.ERROR(
                "No active Unite Us credential; open Unite Us in the browser to "
                "capture one, then re-run."
            ))
            return
        # Read-only probe: never rotate the shared (single-use) refresh token.
        api = uu_api.UniteUsClient(cred, allow_refresh=False)

        if opts["client_id"]:
            clients = Client.objects.filter(client_id__in=opts["client_id"])
        else:
            open_case = Case.objects.filter(
                client=OuterRef("pk"), case_type=CaseType.INTERNAL_SERVICE,
            ).exclude(case_status__in=[CaseStatus.CLOSED, CaseStatus.CANCELLED])
            clients = (
                Client.objects.filter(Exists(open_case))
                .filter(Q(migrated_from_id__isnull=True) | Q(migrated_from_id=""))
            )
        clients = clients.order_by("last_name", "first_name")
        if opts["limit"]:
            clients = clients[: opts["limit"]]

        self.stdout.write(f"{'old_client_id':<38}{'new_client_id':<38}{'gate':<8}name")
        probed = migrated = errors = 0
        for c in clients.iterator(chunk_size=200):
            probed += 1
            try:
                new_id = detect_api_migration(api, c)
            except UniteUsAuthExpired as exc:
                self.stderr.write(self.style.ERROR(
                    f"Unite Us auth expired ({exc}); reconnect in the browser and "
                    f"re-run. Probed {probed - 1} before stopping."
                ))
                break
            except UniteUsApiError as exc:
                errors += 1
                self.stderr.write(f"  ERROR {c.client_id}: {exc}")
                continue
            if not new_id:
                continue
            migrated += 1
            # If the new client is already imported locally, annotate whether the
            # pair passes the strict auto-merge identity gate.
            new_client = resolve_client(new_id)
            if new_client is None:
                gate = "no-local"
            elif new_client.pk == c.pk:
                gate = "self"
            else:
                gate = "MATCH" if identity_matches_for_merge(c, new_client) else "REVIEW"
            name = f"{(c.first_name or '').strip()} {(c.last_name or '').strip()}".strip()
            self.stdout.write(f"  {str(c.client_id):<38}{new_id:<38}{gate:<8}{name}")

        self.stdout.write(self.style.SUCCESS(
            f"\nProbed {probed}; {migrated} migrated on Unite Us; {errors} error(s)."
        ))

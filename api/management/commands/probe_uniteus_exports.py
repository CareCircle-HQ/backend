"""DISCOVERY (temporary): dump the live JSON shapes of the Unite Us Exports API.

The Exports page (app.uniteus.io/exports) is backed by the core API we already
integrate with. This command hits the two read endpoints with an ACTIVE
credential and prints the raw JSON so we can see exactly where the generation
STATUS and the DOWNLOAD URL live -- info the captured HAR lacked (its response
bodies were stripped).

    python manage.py probe_uniteus_exports [--credential-id N] [--provider-id UUID]
                                           [--limit 5] [--export-type clients]

Read-only: it never requests or downloads an export. Safe to delete once the
automation is built.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from api.integrations.uniteus import api as uu_api
from api.models import UniteUsCredential, UniteUsCredentialStatus


class Command(BaseCommand):
    help = "Probe the Unite Us Exports core-API endpoints and print the JSON shapes."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", type=int, default=None,
                             help="Specific UniteUsCredential PK (default: newest ACTIVE).")
        parser.add_argument("--provider-id", default=None,
                             help="Override the requester.provider filter (default: the credential's).")
        parser.add_argument("--export-type", default=None,
                             help="Restrict to one export_type (default: all known types).")
        parser.add_argument("--limit", type=int, default=5,
                             help="How many recent exports to resolve file_uploads for.")
        parser.add_argument("--allow-refresh", action="store_true",
                             help=("Permit a server-side token refresh (ROTATES the "
                                   "single-use refresh token and can log out the live "
                                   "agent). Off by default -- uses the stored token as-is."))

    def handle(self, *args, **opts):
        # Defer the encrypted token columns so *selecting* a credential never
        # eagerly decrypts (a wrong/rotated FIELD_ENCRYPTION_KEY then surfaces
        # only when we actually use the token, with a clean message).
        qs = (UniteUsCredential.objects
              .filter(status=UniteUsCredentialStatus.ACTIVE)
              .defer("access_token", "refresh_token"))
        if opts["credential_id"]:
            qs = qs.filter(pk=opts["credential_id"])
        creds = list(qs.order_by("-last_captured_at", "-updated_at"))
        if not creds:
            raise CommandError("No ACTIVE UniteUsCredential found.")
        self.stdout.write(f"ACTIVE credentials: {len(creds)}")
        for c in creds[:10]:
            self.stdout.write(
                f"  #{c.pk} provider={c.provider_id} employee={c.employee_id} "
                f"captured={c.last_captured_at}"
            )
        cred = creds[0]
        self.stdout.write(f"\nUsing credential #{cred.pk} (allow_refresh={opts['allow_refresh']})\n")

        # Touch the token now so a key mismatch fails with a clear message
        # before we make any live call.
        try:
            _ = cred.access_token
        except Exception as exc:  # noqa: BLE001
            raise CommandError(
                f"Could not decrypt this credential's token ({exc}). The DB was "
                "likely encrypted with a different FIELD_ENCRYPTION_KEY than the "
                "one loaded from .env."
            )

        client = uu_api.UniteUsClient(cred, allow_refresh=opts["allow_refresh"])
        export_types = [opts["export_type"]] if opts["export_type"] else None

        # 1) The exports list (results table).
        try:
            exports = client.list_exports(
                export_types=export_types, provider_id=opts["provider_id"],
            )
        except Exception as exc:  # noqa: BLE001 - discovery: surface anything
            raise CommandError(f"list_exports failed: {exc}")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n===== /v1/exports -> {len(exports)} records ====="
        ))
        if exports:
            self.stdout.write("First export record (full JSON):")
            self.stdout.write(json.dumps(exports[0], indent=2)[:4000])
            self.stdout.write("\nAttribute keys seen across records:")
            keys = sorted({k for e in exports for k in (e.get("attributes") or {})})
            self.stdout.write("  " + ", ".join(keys))
            self.stdout.write("Relationship keys seen across records:")
            rels = sorted({k for e in exports for k in (e.get("relationships") or {})})
            self.stdout.write("  " + (", ".join(rels) or "(none)"))

        # 2) file_uploads (download link + state) for the most recent N.
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n===== /v1/file_uploads per export ====="
        ))
        for e in exports[: opts["limit"]]:
            eid = e.get("id")
            etype = (e.get("attributes") or {}).get("export_type")
            state = (e.get("attributes") or {}).get("state") or (e.get("attributes") or {}).get("status")
            self.stdout.write("-" * 70)
            self.stdout.write(f"export {eid} type={etype} state={state}")
            try:
                fu = client.list_export_file_uploads(eid)
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.WARNING(f"  file_uploads failed: {exc}"))
                continue
            self.stdout.write(json.dumps(fu, indent=2)[:3000])

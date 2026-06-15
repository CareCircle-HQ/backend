"""Diagnose why a screening isn't creating a GoHighLevel opportunity.

Walks the exact path the auto-sync takes and prints what it finds, so we can
see at which step it stops (disabled, no contact id, API error, etc.).

    python manage.py diagnose_screening                 # newest screening, dry run
    python manage.py diagnose_screening <enhanced_id>   # a specific screening
    python manage.py diagnose_screening --send          # actually call the GHL API
"""

import json

import requests
from django.core.management.base import BaseCommand

from api.models import Screening
from api.integrations.ghl import config
from api.integrations.ghl.opportunities import build_screening_payload


class Command(BaseCommand):
    help = "Diagnose GHL screening -> opportunity sync."

    def add_arguments(self, parser):
        parser.add_argument("screen_id", nargs="?", default=None,
                            help="enhanced_screen_id; defaults to the newest screening.")
        parser.add_argument("--send", action="store_true",
                            help="Actually POST to GHL and print the live response.")

    def handle(self, *args, **options):
        # 1) Configuration -------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("1) GHL config"))
        self.stdout.write(f"  is_enabled() : {config.is_enabled()}")
        self.stdout.write(f"  SYNC_ENABLED : {config.SYNC_ENABLED}")
        self.stdout.write(f"  API_BASE     : {config.API_BASE}")
        self.stdout.write(f"  LOCATION_ID  : {config.LOCATION_ID or '(unset)'}")
        self.stdout.write(f"  TOKEN        : {'set' if config.PRIVATE_TOKEN else '(unset)'}")
        if not config.is_enabled():
            self.stderr.write(self.style.ERROR(
                "  -> Sync is DISABLED. sync_screening() returns immediately and no "
                "opportunity is created (no error logged). Set CRM_SYNC_ENABLED=true "
                "+ GHL_PRIVATE_TOKEN + GHL_LOCATION_ID in .env."
            ))

        # 2) Pick a screening ---------------------------------------------
        sid = options["screen_id"]
        if sid:
            screening = Screening.objects.filter(enhanced_screen_id=sid).first()
        else:
            screening = Screening.objects.order_by("-screen_created_at").first()
        if not screening:
            self.stderr.write(self.style.ERROR("No screening found in the DB."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n2) Screening"))
        self.stdout.write(f"  enhanced_screen_id : {screening.enhanced_screen_id}")
        self.stdout.write(f"  screen_type        : {screening.screen_type}")
        self.stdout.write(f"  crm_opportunity_id : {screening.crm_opportunity_id or '(none yet)'}")
        self.stdout.write(f"  crm_synced_at      : {screening.crm_synced_at or '(never)'}")

        client = screening.client
        contact_id = getattr(client, "crm_contact_id", "") if client else ""
        self.stdout.write(self.style.MIGRATE_HEADING("\n3) Linked client / contact"))
        self.stdout.write(f"  client.client_id     : {client.pk if client else '(no client)'}")
        self.stdout.write(f"  client.crm_contact_id: {contact_id or '(none)'}")
        if client and not contact_id:
            self.stderr.write(self.style.ERROR(
                "  -> Client has NO crm_contact_id. The screening is SKIPPED "
                "(build_screening_payload returns None). Save/sync the Profile "
                "first so the contact exists, then re-save the screening."
            ))

        # 4) Payload -------------------------------------------------------
        payload = build_screening_payload(screening)
        self.stdout.write(self.style.MIGRATE_HEADING("\n4) Opportunity payload"))
        if payload is None:
            self.stderr.write(self.style.ERROR(
                "  build_screening_payload() returned None -> skipped (see above)."
            ))
            return
        self.stdout.write(json.dumps(payload, indent=2, default=str))

        # 5) Live call (optional) -----------------------------------------
        if not options["send"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run only. Re-run with --send to actually call the GHL API "
                "and see the real status code / response body."
            ))
            return
        if not config.is_enabled():
            self.stderr.write(self.style.ERROR("\nCannot --send while sync is disabled."))
            return

        body = dict(payload, locationId=config.LOCATION_ID)
        url = f"{config.API_BASE}/opportunities/"
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n5) POST {url}"))
        try:
            resp = requests.post(
                url, headers=config.headers(), json=body, timeout=config.TIMEOUT
            )
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f"Request failed: {exc}"))
            return
        self.stdout.write(f"Status: {resp.status_code}")
        self.stdout.write(resp.text[:1500])
        if resp.status_code in (200, 201):
            self.stdout.write(self.style.SUCCESS(
                "\nOK -- opportunity created. Check the 'B: Screening' pipeline."
            ))
        elif resp.status_code in (401, 403):
            self.stderr.write(self.style.ERROR(
                "\nAuth/scope issue -- the Private Integration token likely lacks "
                "'opportunities.write'. Add it in GHL and regenerate the token."
            ))

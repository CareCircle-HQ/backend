"""Diagnose the GoHighLevel connection.

Confirms the configured Private Integration Token can actually reach the
configured location (the usual cause of a 403 is a token/location mismatch).

    python manage.py check_ghl
"""

import requests
from django.core.management.base import BaseCommand

from api.integrations.ghl import config


class Command(BaseCommand):
    help = "Check GoHighLevel token/location connectivity."

    def handle(self, *args, **options):
        self.stdout.write("GHL configuration:")
        self.stdout.write(f"  CRM_SYNC_ENABLED : {config.SYNC_ENABLED}")
        self.stdout.write(f"  API_BASE         : {config.API_BASE}")
        self.stdout.write(f"  API_VERSION      : {config.API_VERSION}")
        self.stdout.write(f"  LOCATION_ID      : {config.LOCATION_ID or '(unset)'}")
        self.stdout.write(
            f"  TOKEN            : {'set' if config.PRIVATE_TOKEN else '(unset)'}"
        )

        if not config.PRIVATE_TOKEN or not config.LOCATION_ID:
            self.stderr.write(
                self.style.ERROR("Missing token or location id; set them in .env.")
            )
            return

        # Probe the contacts scope we actually use (contacts.readonly), scoped
        # to the configured location. This is the most faithful pairing test.
        url = f"{config.API_BASE}/contacts/"
        params = {"locationId": config.LOCATION_ID, "limit": 1}
        self.stdout.write(f"\nGET {url}?locationId={config.LOCATION_ID}&limit=1")
        try:
            resp = requests.get(
                url, headers=config.headers(), params=params, timeout=config.TIMEOUT
            )
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f"Request failed: {exc}"))
            return

        self.stdout.write(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            self.stdout.write(
                self.style.SUCCESS(
                    "OK -- token can access contacts for this location. "
                    "Contact sync should work."
                )
            )
        elif resp.status_code == 403:
            self.stderr.write(
                self.style.ERROR(
                    "403 -- the token does NOT have access to this location. "
                    "Generate the Private Integration Token inside the SAME "
                    "sub-account whose Location ID you set in GHL_LOCATION_ID."
                )
            )
            self.stderr.write(resp.text[:500])
        elif resp.status_code == 401:
            self.stderr.write(
                self.style.ERROR(
                    "401 -- token missing a required scope. Add 'contacts.readonly' "
                    "and 'contacts.write' to the Private Integration."
                )
            )
            self.stderr.write(resp.text[:500])
        else:
            self.stderr.write(self.style.ERROR(resp.text[:500]))

"""Pull a single contact from GoHighLevel for a local Client and dump every
field as ``field: value`` JSON.

First building block of the inbound "pull household members from GHL" work: it
fetches the GHL contact tied to a local ``Client`` (by ``crm_contact_id``, with
a fallback search by the Enrollment Platform Client ID custom field, then
email/phone), resolves custom-field ids to human names using the catalog in
``tmp/fields_external_crm.csv``, and writes the flattened result to ``tmp/``.

    python manage.py pull_ghl_contact ed8f39ee-f43a-4c62-b023-eb85f4514567
    python manage.py pull_ghl_contact <client_id> --out tmp/her.json

Read-only against GHL; it does NOT honor CRM_SYNC_DISCONNECTED (that gate only
guards OUTBOUND writes). It only needs a valid GHL_PRIVATE_TOKEN (+ location).
"""

import csv
import json
import os

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.integrations.ghl import config
from api.models import Client

# Enrollment Platform Client ID custom field (contact.enrollment_client_id).
ENROLLMENT_CLIENT_ID_FIELD = "xac7ac5fVHKyutg0mrB6"


class Command(BaseCommand):
    help = "Pull a GHL contact for a local Client and dump all fields to tmp/ as JSON."

    def add_arguments(self, parser):
        parser.add_argument("client_id", help="Local Client UUID (pk).")
        parser.add_argument(
            "--out",
            default="",
            help="Output JSON path (defaults to tmp/ghl_contact_<client_id>.json).",
        )

    # -- helpers ------------------------------------------------------------
    def _field_catalog(self):
        """Map GHL custom-field id -> human name for contact-object fields.

        Sourced from tmp/fields_external_crm.csv (Field ID, Field Name, Object).
        Returns {} if the catalog is missing so ids fall back to raw keys.
        """
        path = os.path.join(settings.BASE_DIR, "tmp", "fields_external_crm.csv")
        catalog = {}
        if not os.path.exists(path):
            self.stderr.write(
                self.style.WARNING(
                    f"Field catalog not found at {path}; custom fields will use raw ids."
                )
            )
            return catalog
        # utf-8-sig strips a leading BOM so the first header ("Field ID") is
        # read correctly (otherwise it becomes "\ufeffField ID" and every id is
        # dropped, leaving custom fields unresolved).
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                fid = (row.get("Field ID") or "").strip()
                if fid:
                    catalog[fid] = {
                        "name": (row.get("Field Name") or "").strip(),
                        "key": (row.get("Field Key") or "").strip(),
                        "object": (row.get("Object") or "").strip(),
                    }
        return catalog

    def _live_catalog(self):
        """Fetch the location's live contact custom-field catalog (id -> name)
        from GHL. More complete/current than the static CSV snapshot, so it
        resolves ids the CSV doesn't have. Returns {} on any failure."""
        if not config.LOCATION_ID:
            return {}
        url = f"{config.API_BASE}/locations/{config.LOCATION_ID}/customFields"
        try:
            resp = requests.get(
                url, headers=config.headers(), params={"model": "contact"},
                timeout=config.TIMEOUT,
            )
        except requests.RequestException:
            return {}
        if resp.status_code != 200:
            return {}
        out = {}
        for f in resp.json().get("customFields", []):
            fid = f.get("id")
            if fid:
                out[fid] = {
                    "name": f.get("name") or "",
                    "key": f.get("fieldKey") or "",
                    "object": f.get("model") or "",
                }
        return out

    def _get_contact(self, contact_id):
        url = f"{config.API_BASE}/contacts/{contact_id}"
        resp = requests.get(url, headers=config.headers(), timeout=config.TIMEOUT)
        if resp.status_code != 200:
            raise CommandError(
                f"GET {url} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json().get("contact", resp.json())

    def _search_by_enrollment_id(self, client):
        """Most reliable lookup: match the Enrollment Platform Client ID custom
        field (contact.enrollment_client_id) to the local Client UUID via the
        GHL Search Contacts endpoint. Returns a contact id or None."""
        if not config.LOCATION_ID:
            return None
        url = f"{config.API_BASE}/contacts/search"
        body = {
            "locationId": config.LOCATION_ID,
            "page": 1,
            "pageLimit": 20,
            "filters": [
                {
                    "field": ENROLLMENT_CLIENT_ID_FIELD,
                    "operator": "eq",
                    "value": str(client.pk),
                }
            ],
        }
        try:
            resp = requests.post(
                url, headers=config.headers(), json=body, timeout=config.TIMEOUT
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            self.stderr.write(
                self.style.WARNING(
                    f"  enrollment-id search {resp.status_code}: {resp.text[:200]}"
                )
            )
            return None
        for c in resp.json().get("contacts", []):
            if c.get("id"):
                return c["id"]
        return None

    def _search_by_query(self, client):
        """Fallback lookup: the location's general contact query by enrollment
        UUID, then email, then phone."""
        if not config.LOCATION_ID:
            return None
        url = f"{config.API_BASE}/contacts/"
        for q in (
            str(client.pk),
            client.client_email_address,
            client.client_phone_number,
        ):
            if not q:
                continue
            resp = requests.get(
                url,
                headers=config.headers(),
                params={"locationId": config.LOCATION_ID, "query": q, "limit": 20},
                timeout=config.TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            for c in resp.json().get("contacts", []):
                if c.get("id"):
                    return c["id"]
        return None

    def _search_contact_id(self, client):
        """Resolve a GHL contact id for a Client without crm_contact_id: first by
        the Enrollment Platform Client ID custom field, then by email/phone."""
        return self._search_by_enrollment_id(client) or self._search_by_query(client)

    def _flatten(self, contact, catalog):
        """Flatten the GHL contact into {field_name: value}: scalar standard
        fields at the top level + custom fields resolved to human names."""
        out = {}
        custom_raw = contact.get("customFields") or contact.get("customField") or []
        for key, value in contact.items():
            if key in ("customFields", "customField"):
                continue
            # Keep scalars/lists/strings; skip nested objects we expand elsewhere.
            out[key] = value

        custom = {}
        for cf in custom_raw:
            fid = cf.get("id") or cf.get("fieldId") or ""
            value = cf.get("value")
            if value is None:
                value = cf.get("field_value", cf.get("fieldValue"))
            meta = catalog.get(fid)
            label = (meta["name"] if meta and meta["name"] else None) or fid
            custom[label] = value
        if custom:
            out["custom_fields"] = custom
        return out

    # -- main ---------------------------------------------------------------
    def handle(self, *args, **options):
        if not config.PRIVATE_TOKEN:
            raise CommandError("GHL_PRIVATE_TOKEN is not set; configure it in .env.")

        client_id = options["client_id"].strip()
        try:
            client = Client.objects.get(pk=client_id)
        except (Client.DoesNotExist, ValueError):
            raise CommandError(f"No local Client found for id {client_id!r}.")

        contact_id = (client.crm_contact_id or "").strip()
        if contact_id:
            self.stdout.write(f"Using Client.crm_contact_id = {contact_id}")
        else:
            self.stdout.write(
                "Client has no crm_contact_id; searching GHL by email/phone…"
            )
            contact_id = self._search_contact_id(client)
            if not contact_id:
                raise CommandError(
                    "Could not resolve a GHL contact for this client "
                    "(no crm_contact_id and no email/phone match)."
                )
            self.stdout.write(f"Matched GHL contact = {contact_id}")

        # CSV snapshot as a base, then overlay the live GHL catalog so every
        # custom-field id resolves to its current human name.
        catalog = self._field_catalog()
        catalog.update(self._live_catalog())
        contact = self._get_contact(contact_id)
        flat = self._flatten(contact, catalog)

        result = {
            "_meta": {
                "client_id": str(client.pk),
                "client_name": f"{client.first_name} {client.last_name}".strip(),
                "ghl_contact_id": contact_id,
                "fetched_at": timezone.now().isoformat(),
                "source": "GoHighLevel /contacts/{id}",
            },
            **flat,
        }

        out_path = options["out"] or os.path.join(
            settings.BASE_DIR, "tmp", f"ghl_contact_{client_id}.json"
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False, default=str)

        n_custom = len(flat.get("custom_fields", {}))
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {out_path} ({n_custom} custom fields resolved)."
            )
        )

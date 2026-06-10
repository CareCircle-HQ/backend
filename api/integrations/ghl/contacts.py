"""Push a local ``Client`` to GoHighLevel as a Contact.

Synchronous (``requests``) so we can capture the returned contact id and write
it back to ``Client.crm_contact_id`` in the same request. Every failure is
caught and logged -- this must never break a local save.
"""

import hashlib
import json
import logging

import requests
from django.utils import timezone

from . import config
from .custom_fields import build_custom_fields

logger = logging.getLogger(__name__)

# GHL accepts a limited gender vocabulary; only forward known values.
_GHL_GENDERS = {"male", "female"}

# Fields accepted by the create/upsert endpoints but rejected by the update
# (PUT) endpoint with a 422 ("property X should not exist"). Stripped on update.
_UPDATE_EXCLUDED_FIELDS = {"gender"}


def _primary_address(client):
    """Best delivery/current address: prefer active, else the first on file."""
    addresses = list(client.addresses.all())
    if not addresses:
        return None
    for addr in addresses:
        if addr.is_active:
            return addr
    return addresses[0]


def build_contact_payload(client):
    """Map a ``Client`` to a GHL contact body. Empty values are omitted so an
    update never blanks out a field the CRM already has.
    """
    payload = {}

    def put(key, value):
        if value not in (None, ""):
            payload[key] = value

    put("firstName", client.first_name)
    put("lastName", client.last_name)
    name = f"{client.first_name or ''} {client.last_name or ''}".strip()
    put("name", name)
    put("email", client.client_email_address)
    put("phone", client.client_phone_number)

    if client.date_of_birth:
        put("dateOfBirth", client.date_of_birth.isoformat())

    gender = (client.gender or "").lower()
    if gender in _GHL_GENDERS:
        put("gender", gender)

    addr = _primary_address(client)
    if addr:
        line = " ".join(p for p in [addr.line1, addr.line2] if p).strip()
        put("address1", line)
        put("city", addr.city)
        put("state", addr.state)
        put("postalCode", addr.postal_code)
    put("country", "US")

    put("source", config.CONTACT_SOURCE)

    custom = build_custom_fields(client)
    if custom:
        payload["customFields"] = custom
    return payload


def _payload_hash(payload):
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _extract_contact_id(data):
    if not isinstance(data, dict):
        return None
    contact = data.get("contact")
    if isinstance(contact, dict) and contact.get("id"):
        return contact["id"]
    return data.get("id")


def sync_client(client):
    """Create/update the client's GHL contact and write the id back locally.

    Returns the contact id on success, or ``None`` if disabled, skipped, or on
    error (errors are logged, never raised).
    """
    if not config.is_enabled():
        return None

    payload = build_contact_payload(client)
    new_hash = _payload_hash(payload)

    # Nothing changed since the last successful push -> skip the round trip.
    if client.crm_contact_id and client.crm_sync_hash == new_hash:
        return client.crm_contact_id

    try:
        if client.crm_contact_id:
            # Update an existing contact (locationId is fixed by the contact).
            # The update endpoint rejects some create-only fields (e.g. gender).
            update_body = {
                k: v for k, v in payload.items() if k not in _UPDATE_EXCLUDED_FIELDS
            }
            url = f"{config.API_BASE}/contacts/{client.crm_contact_id}"
            resp = requests.put(
                url, headers=config.headers(), json=update_body, timeout=config.TIMEOUT
            )
        else:
            body = dict(payload, locationId=config.LOCATION_ID)
            if payload.get("email") or payload.get("phone"):
                # Upsert dedupes by email/phone within the location, avoiding
                # duplicate contacts on repeated extension saves.
                url = f"{config.API_BASE}/contacts/upsert"
            else:
                url = f"{config.API_BASE}/contacts/"
            resp = requests.post(
                url, headers=config.headers(), json=body, timeout=config.TIMEOUT
            )

        resp.raise_for_status()
        contact_id = _extract_contact_id(resp.json())
        if not contact_id:
            logger.warning(
                "GHL sync for client %s succeeded but returned no contact id: %s",
                client.pk, resp.text[:300],
            )
            return None

        client.crm_contact_id = contact_id
        client.crm_sync_hash = new_hash
        client.crm_synced_at = timezone.now()
        client.save(
            update_fields=["crm_contact_id", "crm_sync_hash", "crm_synced_at"]
        )
        return contact_id

    except requests.RequestException as exc:
        body = getattr(exc.response, "text", "")[:300] if exc.response else ""
        logger.warning(
            "GHL contact sync failed for client %s: %s %s", client.pk, exc, body
        )
        return None
    except Exception:  # never let CRM issues break the local save
        logger.exception("Unexpected error syncing client %s to GHL", client.pk)
        return None

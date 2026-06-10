"""Configuration + low-level HTTP helpers for the GoHighLevel REST API.

Reads from Django settings (which load the values from the environment). The
LeadConnector API requires a ``Version`` header in addition to the bearer token.
"""

from django.conf import settings

# LeadConnector / GoHighLevel REST base + required API version header value.
API_BASE = getattr(
    settings, "GHL_API_BASE", "https://services.leadconnectorhq.com"
).rstrip("/")
API_VERSION = getattr(settings, "GHL_API_VERSION", "2021-07-28")

PRIVATE_TOKEN = getattr(settings, "GHL_PRIVATE_TOKEN", "")
LOCATION_ID = getattr(settings, "GHL_LOCATION_ID", "")

# Master switch. When false, sync calls are complete no-ops.
SYNC_ENABLED = getattr(settings, "CRM_SYNC_ENABLED", False)

# Network timeout (seconds) for every CRM request.
TIMEOUT = getattr(settings, "GHL_TIMEOUT", 10)

# Value written to the GHL contact ``source`` field.
# Previously defaulted to "Benefully extension", now blank to be captured from enrollment form.
CONTACT_SOURCE = getattr(settings, "GHL_CONTACT_SOURCE", "")


def is_enabled():
    """True only when the integration is switched on AND minimally configured."""
    return bool(SYNC_ENABLED and PRIVATE_TOKEN and LOCATION_ID)


def headers():
    return {
        "Authorization": f"Bearer {PRIVATE_TOKEN}",
        "Version": API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

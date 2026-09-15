"""Configuration + auth headers for the CallTools REST API.

Reads from Django settings (loaded from the environment). CallTools authenticates
with an API key sent as ``Authorization: Token <token>``. The base URL is the
per-silo subdomain of calltools.io, e.g. ``https://east-1.calltools.io/api``.
"""

from django.conf import settings

API_BASE = getattr(
    settings, "CALLTOOLS_API_BASE", "https://east-1.calltools.io/api"
).rstrip("/")

API_TOKEN = getattr(settings, "CALLTOOLS_API_TOKEN", "")

# Network timeout (seconds) for every CallTools request.
# Presence is polled every 10s by every open side panel, and each request can
# make TWO upstream calls, so the timeout is a per-request worker-hold budget --
# not just "how patient are we". At 15s a slow CallTools held a gunicorn thread
# for up to 30s and spiked p99 to 19.5s (CloudWatch alarm, 2026-09-15 03:35).
# A presence dot is worth ~3s; beyond that "unknown" is the better answer.
TIMEOUT = getattr(settings, "CALLTOOLS_TIMEOUT", 3)


def is_enabled():
    """True only when an API token is configured."""
    return bool(API_TOKEN)


def headers():
    return {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

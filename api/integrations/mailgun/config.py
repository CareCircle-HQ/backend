"""Configuration for the Mailgun messages API.

Reads from Django settings (loaded from the environment). Mailgun authenticates
with HTTP basic auth: username ``api`` + the private API key. Messages are sent
to ``{API_BASE}/v3/{DOMAIN}/messages`` where DOMAIN is the sending domain (a
sandbox ``sandboxXXXX.mailgun.org`` for testing, or a verified domain in prod).
"""

from django.conf import settings

API_BASE = getattr(settings, "MAILGUN_API_BASE", "https://api.mailgun.net").rstrip("/")

API_KEY = getattr(settings, "MAILGUN_API_KEY", "")

DOMAIN = getattr(settings, "MAILGUN_DOMAIN", "")

# From header. Defaults to postmaster@<domain> when not explicitly configured.
FROM = getattr(settings, "MAILGUN_FROM", "") or (
    f"CareCircle <postmaster@{DOMAIN}>" if DOMAIN else ""
)

# Network timeout (seconds) for every Mailgun request.
TIMEOUT = getattr(settings, "MAILGUN_TIMEOUT", 15)


def is_enabled():
    """True only when an API key and a sending domain are configured."""
    return bool(API_KEY and DOMAIN)

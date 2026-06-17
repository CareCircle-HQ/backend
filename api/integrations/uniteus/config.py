"""Unite Us integration configuration (env-driven via settings)."""

from django.conf import settings


def is_enabled():
    return bool(getattr(settings, "UNITEUS_ENABLED", False))


def token_url():
    return getattr(settings, "UNITEUS_TOKEN_URL", "") or ""


def client_id():
    return getattr(settings, "UNITEUS_CLIENT_ID", "") or ""


def client_secret():
    return getattr(settings, "UNITEUS_CLIENT_SECRET", "") or ""


def api_base():
    return getattr(settings, "UNITEUS_API_BASE", "") or ""


def timeout():
    return int(getattr(settings, "UNITEUS_TIMEOUT", 30))


def refresh_skew():
    return int(getattr(settings, "UNITEUS_REFRESH_SKEW", 120))

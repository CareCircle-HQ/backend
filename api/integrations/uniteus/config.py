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


def screenings_ingestion_base():
    """Host the assessment/screening RESULTS live on (a different host than the
    core JSON:API). The extension reads eligible_services from here."""
    return (
        getattr(settings, "UNITEUS_SCREENINGS_INGESTION_BASE", "")
        or "https://screenings-ingestion.uniteus.io"
    )


def timeout():
    return int(getattr(settings, "UNITEUS_TIMEOUT", 30))


def refresh_skew():
    return int(getattr(settings, "UNITEUS_REFRESH_SKEW", 120))

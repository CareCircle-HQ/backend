"""Unite Us OAuth token refresh.

The daily pull calls ``ensure_fresh(cred)`` before using a credential. If the
access token is near/at expiry it is refreshed via the ``refresh_token`` grant.
On unrecoverable failure (no/expired/revoked refresh token) the credential is
marked EXPIRED and the caller is expected to raise a "re-login" ticket.

Endpoint specifics (URL, client_id, secret/PKCE, rotation) come from settings;
confirm them via the extension's auth capture (window.__uwCapture(true)).
"""

import logging
from datetime import timedelta

import requests
from django.utils import timezone

from api.models import UniteUsCredentialStatus
from . import config

logger = logging.getLogger(__name__)


class UniteUsAuthError(Exception):
    """Raised for configuration/transport errors (not a normal token rejection)."""


def needs_refresh(cred):
    if not cred.access_token:
        return True
    # No known expiry (the extension often captures the bearer from a request
    # header, so the OAuth ``expires_in`` is absent and ``access_expires_at``
    # is null): treat a freshly captured token as usable rather than forcing a
    # refresh we may not be able to perform. If it has actually expired the
    # live API call returns 401/403 and the UniteUsAuthExpired re-login path
    # takes over.
    if not cred.access_expires_at:
        return False
    return cred.access_expires_at <= timezone.now() + timedelta(
        seconds=config.refresh_skew()
    )


# Unite Us refresh tokens ROTATE (single-use): the server and the live browser
# session share ONE rotating chain, so if the server refreshes while an agent is
# actively working in Unite Us, it invalidates the browser's copy and forces a
# re-login. While a capture is recent the extension is keeping the token fresh
# for us anyway, so we skip the server-side refresh and use the captured token.
CAPTURE_FRESH_WINDOW = timedelta(minutes=15)


def recently_captured(cred):
    """True when the extension pushed a session within CAPTURE_FRESH_WINDOW --
    i.e. an agent is actively in Unite Us and the token is being kept fresh for
    us. In that window we must not steal a rotation with a server-side refresh."""
    return (
        cred.last_captured_at is not None
        and timezone.now() - cred.last_captured_at < CAPTURE_FRESH_WINDOW
    )


def _mark_expired(cred):
    cred.status = UniteUsCredentialStatus.EXPIRED
    cred.save(update_fields=["status", "updated_at"])


def refresh_credential(cred):
    """Refresh ``cred`` in place. Returns True on success; marks it EXPIRED and
    returns False on a token rejection. Raises UniteUsAuthError on config/transport
    errors so the run can distinguish 'agent must re-login' from 'infra problem'."""
    if not cred.refresh_token:
        _mark_expired(cred)
        return False

    url = config.token_url()
    if not url:
        # No OAuth token endpoint configured: we can't refresh server-side, so
        # mark the credential EXPIRED and let the caller fall back to the
        # re-login path (an agent re-capturing a fresh token via the extension)
        # instead of raising a config error for every person in the pull.
        logger.warning(
            "Unite Us token refresh skipped: UNITEUS_TOKEN_URL not configured "
            "(credential %s marked expired)", cred.pk,
        )
        _mark_expired(cred)
        return False

    data = {"grant_type": "refresh_token", "refresh_token": cred.refresh_token}
    if config.client_id():
        data["client_id"] = config.client_id()
    if config.client_secret():
        data["client_secret"] = config.client_secret()

    try:
        resp = requests.post(url, data=data, timeout=config.timeout())
    except requests.RequestException as exc:
        logger.warning("Unite Us token refresh request failed: %s", exc)
        raise UniteUsAuthError(str(exc))

    if resp.status_code != 200:
        logger.warning(
            "Unite Us token refresh rejected (%s): %s",
            resp.status_code, resp.text[:300],
        )
        _mark_expired(cred)
        return False

    try:
        payload = resp.json()
    except ValueError as exc:
        raise UniteUsAuthError(f"Unite Us token response was not JSON: {exc}")

    cred.access_token = payload.get("access_token") or cred.access_token
    if payload.get("refresh_token"):  # rotation
        cred.refresh_token = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if expires_in:
        cred.access_expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    if payload.get("scope"):
        cred.scope = payload["scope"]
    if payload.get("token_type"):
        cred.token_type = payload["token_type"]
    cred.status = UniteUsCredentialStatus.ACTIVE
    cred.last_refreshed_at = timezone.now()
    cred.save()
    return True


def ensure_fresh(cred):
    """Ensure ``cred`` has a usable access token. Returns True if usable."""
    if cred.status == UniteUsCredentialStatus.REVOKED:
        return False
    if not needs_refresh(cred):
        return True
    # Don't steal a rotation from an actively-working agent: if the extension
    # captured a session very recently the browser is keeping the (shared,
    # single-use) refresh-token chain alive, so a server-side refresh here would
    # invalidate the agent's session. Use the captured token as-is; if it has
    # truly expired the live call 401s and the UniteUsAuthExpired re-login path
    # takes over. Off-hours (no recent capture) we refresh normally.
    if recently_captured(cred):
        return bool(cred.access_token)
    return refresh_credential(cred)


def auth_headers(cred):
    """Headers to call the Unite Us API with this credential."""
    return {
        "Authorization": f"{cred.token_type or 'Bearer'} {cred.access_token}",
        "x-employee-id": cred.employee_id or "",
        "x-provider-id": cred.provider_id or "",
    }

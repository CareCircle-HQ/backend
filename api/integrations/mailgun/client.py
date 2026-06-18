"""Low-level HTTP helper for the Mailgun messages API.

Synchronous (``requests``). One public function, :func:`send_email`, which POSTs
a single message and returns Mailgun's decoded JSON response (carries the queued
message id). Raises :class:`MailgunError` on misconfiguration or a non-2xx
response so callers can decide how to surface the failure.
"""

import logging

import requests

from . import config

logger = logging.getLogger(__name__)


class MailgunError(RuntimeError):
    """Raised when a Mailgun request fails (not configured, transport, or non-2xx)."""


def send_email(to, subject, text, html=None, from_addr=None):
    """Send a single email via Mailgun.

    Args:
        to: recipient address, or a list of addresses. Each may be a bare
            address or a "Name <addr>" string.
        subject: message subject.
        text: plain-text body (always sent).
        html: optional HTML body.
        from_addr: optional From override; defaults to ``config.FROM``.

    Returns:
        The decoded JSON response (e.g. ``{"id": "...", "message": "Queued..."}``).
    """
    if not config.is_enabled():
        raise MailgunError(
            "Mailgun is not configured (set MAILGUN_API_KEY and MAILGUN_DOMAIN)."
        )

    url = f"{config.API_BASE}/v3/{config.DOMAIN}/messages"
    data = {
        "from": from_addr or config.FROM,
        "to": ", ".join(to) if isinstance(to, (list, tuple)) else to,
        "subject": subject,
        "text": text,
    }
    if html:
        data["html"] = html

    try:
        resp = requests.post(
            url, auth=("api", config.API_KEY), data=data, timeout=config.TIMEOUT
        )
    except requests.RequestException as exc:
        raise MailgunError(f"Mailgun request failed: {exc}") from exc

    if resp.status_code >= 400:
        # Never log the body at INFO (may echo recipient); keep it to the error.
        raise MailgunError(
            f"Mailgun {resp.status_code} for POST {url}: {resp.text[:300]}"
        )

    return resp.json() if resp.content else {}

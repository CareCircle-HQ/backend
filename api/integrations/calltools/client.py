"""Low-level HTTP helpers for the CallTools REST API.

Synchronous (``requests``). CallTools is a DRF-style API: list endpoints return
``{"count", "next", "previous", "results"}`` and paginate via the ``next`` URL.
"""

import logging

import requests

from . import config

logger = logging.getLogger(__name__)


class CallToolsError(RuntimeError):
    """Raised when a CallTools request fails (non-2xx or transport error)."""


def _url(path):
    """Join a relative path onto the configured API base."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{config.API_BASE}/{path.lstrip('/')}"


def get(path, params=None):
    """GET a single CallTools resource/page and return the decoded JSON."""
    if not config.is_enabled():
        raise CallToolsError("CallTools API token not configured (CALLTOOLS_API_TOKEN).")
    url = _url(path)
    try:
        resp = requests.get(
            url, headers=config.headers(), params=params, timeout=config.TIMEOUT
        )
    except requests.RequestException as exc:
        raise CallToolsError(f"CallTools request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise CallToolsError(
            f"CallTools {resp.status_code} for GET {url}: {resp.text[:500]}"
        )
    if not resp.content:
        return {}
    return resp.json()


def get_all(path, params=None, max_pages=100):
    """Follow DRF ``next`` links and return the concatenated ``results`` list.

    Falls back to returning the raw payload (wrapped in a list) when the response
    is not a paginated envelope.
    """
    items = []
    page = get(path, params=params)
    pages = 0
    while True:
        pages += 1
        if isinstance(page, dict) and "results" in page:
            items.extend(page.get("results") or [])
            nxt = page.get("next")
            if not nxt or pages >= max_pages:
                break
            page = get(nxt)
        elif isinstance(page, list):
            items.extend(page)
            break
        else:
            items.append(page)
            break
    return items

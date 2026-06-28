"""CallTools live presence + active-call helpers.

Two upstream endpoints back this module:

- ``/userstatuses/{app_user}/`` -- login presence: ``logged_in`` + timestamps.
- ``/livephonecalls/`` -- calls currently in progress, keyed by ``app_user``.
  The other party's number is ``source`` for inbound calls and ``destination``
  for outbound ones.

The live-calls list is cached briefly (a few seconds) in Django's cache so that
many agents polling at once share a single upstream fetch.
"""

import logging

from django.core.cache import cache

from . import client

logger = logging.getLogger(__name__)

USER_STATUS_PATH = "/userstatuses/"
LIVE_CALLS_PATH = "/livephonecalls/"

_LIVE_CALLS_CACHE_KEY = "calltools:live_calls"
_LIVE_CALLS_TTL = 8  # seconds


def get_user_status(app_user):
    """Return the raw ``/userstatuses/{app_user}/`` dict (or ``{}``)."""
    if not app_user:
        return {}
    data = client.get(f"{USER_STATUS_PATH}{app_user}/")
    return data if isinstance(data, dict) else {}


def list_live_calls(use_cache=True):
    """Return all in-progress calls. Cached for a few seconds across callers."""
    if use_cache:
        cached = cache.get(_LIVE_CALLS_CACHE_KEY)
        if cached is not None:
            return cached
    calls = client.get_all(LIVE_CALLS_PATH)
    cache.set(_LIVE_CALLS_CACHE_KEY, calls, _LIVE_CALLS_TTL)
    return calls


def _other_party(call):
    """The number of the non-agent party on a call.

    Inbound: the caller is ``source`` (``destination`` is our DID).
    Outbound: the callee is ``destination`` (``source`` is our caller id).
    """
    if call.get("inbound"):
        return call.get("source") or ""
    return call.get("destination") or ""


def _normalize_number(value):
    """Reduce a phone number to comparable digits (last 10, US-style)."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def numbers_match(a, b):
    """True when two phone numbers share the same last-10-digit form."""
    na, nb = _normalize_number(a), _normalize_number(b)
    return bool(na) and na == nb


def get_active_call(app_user, calls=None):
    """Return a trimmed active-call dict for ``app_user``, or ``None``.

    Picks the most recently started call when an agent has more than one leg.
    """
    if not app_user:
        return None
    calls = list_live_calls() if calls is None else calls
    mine = [
        c for c in calls
        if isinstance(c, dict) and str(c.get("app_user")) == str(app_user)
    ]
    if not mine:
        return None
    call = max(mine, key=lambda c: c.get("start") or "")
    number = _other_party(call)
    return {
        "call_uuid": call.get("call_uuid"),
        "number": number,
        "direction": "inbound" if call.get("inbound") else "outbound",
        "call_type": call.get("call_type"),
        "campaign": call.get("campaign"),
        "queue": call.get("queue"),
        "contact": call.get("contact"),
        "started_at": call.get("start"),
        "answered_at": call.get("answered_on"),
    }


def agent_presence(app_user, client_phone=None):
    """Combine login status + live call into a single presence snapshot.

    ``status`` is one of ``on_call`` / ``online`` / ``offline`` / ``unknown``.
    When ``client_phone`` is given, ``active_call.matches_client`` is set.
    """
    snapshot = {
        "app_user": str(app_user) if app_user else None,
        "logged_in": False,
        "on_call": False,
        "status": "unknown",
        "active_call": None,
    }
    if not app_user:
        return snapshot

    try:
        status = get_user_status(app_user)
        snapshot["logged_in"] = bool(status.get("logged_in"))
        snapshot["logged_in_since"] = status.get("logged_in_since")
    except client.CallToolsError as exc:
        logger.warning("CallTools userstatus fetch failed for %s: %s", app_user, exc)

    try:
        active = get_active_call(app_user)
    except client.CallToolsError as exc:
        logger.warning("CallTools live calls fetch failed: %s", exc)
        active = None

    if active:
        snapshot["on_call"] = True
        if client_phone is not None:
            active["matches_client"] = numbers_match(active.get("number"), client_phone)
        snapshot["active_call"] = active

    if snapshot["on_call"]:
        snapshot["status"] = "on_call"
    elif snapshot["logged_in"]:
        snapshot["status"] = "online"
    else:
        snapshot["status"] = "offline"
    return snapshot

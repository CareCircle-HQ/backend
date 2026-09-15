"""CallTools live presence + active-call helpers.

Two upstream endpoints back this module:

- ``/userstatuses/{app_user}/`` -- login presence: ``logged_in`` + timestamps.
- ``/livephonecalls/`` -- calls currently in progress, keyed by ``app_user``.
  The other party's number is ``source`` for inbound calls and ``destination``
  for outbound ones.

Both are cached briefly (a few seconds) in Django's cache so that many agents
polling at once share a single upstream fetch.

Why the caching matters more than it looks: the extension side panel polls
``/api/calltools/status/`` every 10 SECONDS for every signed-in agent, and that
view calls straight through to CallTools. An unbounded upstream call inside a
polled request is how a third party's bad afternoon becomes our outage -- with 9
workers x 2 threads, twenty agents polling while CallTools is slow saturates every
worker in under a minute. A CloudWatch p99 alarm caught exactly this at 03:35 on
2026-09-15 (19.5s, from two 15s-timeout calls per request).

FAILURES ARE CACHED TOO, briefly: when CallTools is down, the failure path is the
expensive one, and re-asking on every poll is what keeps the workers busy.
"""

import logging

from django.core.cache import cache

from . import client

logger = logging.getLogger(__name__)

USER_STATUS_PATH = "/userstatuses/"
LIVE_CALLS_PATH = "/livephonecalls/"

_LIVE_CALLS_CACHE_KEY = "calltools:live_calls"
_LIVE_CALLS_TTL = 8  # seconds
_USER_STATUS_TTL = 8  # seconds -- shorter than the 10s poll, so it stays fresh
# A failed upstream call is cached for longer than a successful one: while
# CallTools is down every poll would otherwise pay the full timeout again.
_FAILURE_TTL = 20


def _user_status_key(app_user):
    return f"calltools:user_status:{app_user}"


def get_user_status(app_user, use_cache=True):
    """Return the raw ``/userstatuses/{app_user}/`` dict (or ``{}``).

    Cached like :func:`list_live_calls`: the side panel polls every 10s per agent,
    so without this every poll is an uncached upstream round trip holding a
    gunicorn thread for up to the client timeout.
    """
    if not app_user:
        return {}
    key = _user_status_key(app_user)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            # A cached failure is stored as {} -- see below.
            return cached
    try:
        data = client.get(f"{USER_STATUS_PATH}{app_user}/")
    except client.CallToolsError:
        # Remember the failure briefly so a CallTools outage costs one timeout per
        # _FAILURE_TTL, not one per poll per agent.
        cache.set(key, {}, _FAILURE_TTL)
        raise
    data = data if isinstance(data, dict) else {}
    cache.set(key, data, _USER_STATUS_TTL)
    return data


def list_live_calls(use_cache=True):
    """Return all in-progress calls. Cached for a few seconds across callers."""
    if use_cache:
        cached = cache.get(_LIVE_CALLS_CACHE_KEY)
        if cached is not None:
            return cached
    try:
        calls = client.get_all(LIVE_CALLS_PATH)
    except client.CallToolsError:
        # Same reasoning as get_user_status: cache the failure so a slow/broken
        # CallTools is paid for once per _FAILURE_TTL rather than once per poll.
        cache.set(_LIVE_CALLS_CACHE_KEY, [], _FAILURE_TTL)
        raise
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

    status_known = False
    try:
        status = get_user_status(app_user)
        snapshot["logged_in"] = bool(status.get("logged_in"))
        snapshot["logged_in_since"] = status.get("logged_in_since")
        status_known = True
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
    elif status_known:
        snapshot["status"] = "offline"
    # else: leave "unknown" -- we could not ASK CallTools, which is not the same
    # as being told the agent is logged out. This block used to overwrite it
    # unconditionally, so an upstream outage reported every agent as offline and
    # the documented "unknown" state was unreachable.
    return snapshot

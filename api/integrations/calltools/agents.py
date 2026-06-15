"""CallTools agents / users.

The CallTools account roster lives at ``/users/`` (DRF-paginated). Each record
carries ``app_user`` (uuid id), ``full_name``, ``email``, ``extension`` (the
dialer extension, which doubles as our agent code) and role flags such as
``is_agent`` / ``is_manager``.
"""

from . import client

USERS_PATH = "/users/"


def list_users(params=None):
    """Return every user/account member from CallTools (follows pagination)."""
    return client.get_all(USERS_PATH, params=params)


def list_agents(params=None):
    """Return only the users flagged as agents (``is_agent`` is true)."""
    return [u for u in list_users(params=params) if isinstance(u, dict) and u.get("is_agent")]

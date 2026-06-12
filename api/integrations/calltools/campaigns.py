"""CallTools campaigns.

Campaigns live at ``/campaigns/`` (DRF-paginated). Each record carries an
integer ``id``, a ``uuid``, a ``name``, an ``active`` flag, plus a large bag of
dialer settings we don't need here. ``list_campaign_options`` trims the payload
down to what the extension needs to populate the Lead Source dropdown.
"""

from . import client

CAMPAIGNS_PATH = "/campaigns/"


def list_campaigns(params=None):
    """Return the raw campaign dicts from CallTools (follows pagination)."""
    return client.get_all(CAMPAIGNS_PATH, params=params)


def list_campaign_options(active_only=False):
    """Return ``[{id, uuid, name, active}]`` for the Lead Source dropdown.

    Sorted with active campaigns first, then alphabetically by name.
    """
    options = []
    for c in list_campaigns():
        if not isinstance(c, dict):
            continue
        active = bool(c.get("active"))
        if active_only and not active:
            continue
        options.append(
            {
                "id": c.get("id"),
                "uuid": c.get("uuid"),
                "name": c.get("name") or str(c.get("id") or ""),
                "active": active,
            }
        )
    options.sort(key=lambda o: (not o["active"], (o["name"] or "").lower()))
    return options

"""CallTools campaigns.

Campaigns live at ``/campaigns/`` (DRF-paginated), alongside queues. Like the
queue list, ``list_campaign_options`` trims the payload to what the Lead Source
dropdown needs. Campaign records carry an ``active`` flag (unlike queues, whose
flag is unreliable), so we honour it directly.
"""

from . import client

CAMPAIGNS_PATH = "/campaigns/"


def list_campaigns(params=None):
    """Return the raw campaign dicts from CallTools (follows pagination)."""
    return client.get_all(CAMPAIGNS_PATH, params=params)


def list_campaign_options(active_only=False):
    """Return ``[{id, uuid, name, active}]`` for the Lead Source dropdown.

    Sorted with active campaigns first, then alphabetically by name. Missing
    ``active`` flags are treated as active so the campaign still shows.
    """
    options = []
    for c in list_campaigns():
        if not isinstance(c, dict):
            continue
        raw_active = c.get("active")
        active = True if raw_active is None else bool(raw_active)
        if active_only and not active:
            continue
        options.append(
            {
                "id": c.get("id"),
                "uuid": c.get("uuid"),
                "name": c.get("name") or c.get("campaign_name") or str(c.get("id") or ""),
                "active": active,
            }
        )
    options.sort(key=lambda o: (not o["active"], (o["name"] or "").lower()))
    return options

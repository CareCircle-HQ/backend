"""CallTools queues.

Queues live at ``/queues/`` (DRF-paginated). Each record carries an integer
``id``, a ``name``, and a bag of routing settings we don't need here.
``list_queue_options`` trims the payload down to what the extension needs to
populate the Lead Source dropdown.
"""

from . import client

QUEUES_PATH = "/queues/"


def list_queues(params=None):
    """Return the raw queue dicts from CallTools (follows pagination)."""
    return client.get_all(QUEUES_PATH, params=params)


def list_queue_options(active_only=False):
    """Return ``[{id, uuid, name, active}]`` for the Lead Source dropdown.

    Sorted with active queues first, then alphabetically by name. CallTools
    queue records don't reliably carry an ``active`` flag (unlike campaigns);
    when it is absent we treat the queue as active so it still shows.
    """
    options = []
    for q in list_queues():
        if not isinstance(q, dict):
            continue
        raw_active = q.get("active")
        active = True if raw_active is None else bool(raw_active)
        if active_only and not active:
            continue
        options.append(
            {
                "id": q.get("id"),
                "uuid": q.get("uuid"),
                "name": q.get("name") or q.get("queue_name") or str(q.get("id") or ""),
                "active": active,
            }
        )
    options.sort(key=lambda o: (not o["active"], (o["name"] or "").lower()))
    return options

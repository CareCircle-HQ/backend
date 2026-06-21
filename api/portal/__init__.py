"""Customer-support web portal API layer.

A self-contained `/api/portal/` namespace consumed by the React support
frontend (frontend/). Kept separate from the Chrome-extension API (api.views,
api.urls) so the two can evolve independently — the extension endpoints are
never modified by portal work.
"""

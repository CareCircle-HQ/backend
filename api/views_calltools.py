"""CallTools-backed read endpoints for the extension.

Currently exposes the campaign list used to populate the Profile tab's Lead
Source dropdown. Results are cached briefly so we don't hit CallTools on every
panel open.
"""

import logging

from django.core.cache import cache
from rest_framework import permissions, status, views
from rest_framework.response import Response

from .integrations.calltools import campaigns, client, config, presence
from .models import Agent

logger = logging.getLogger(__name__)

_CACHE_KEY = "calltools:campaign_options"
_CACHE_TTL = 300  # seconds


class CallToolsCampaignsView(views.APIView):
    """GET /api/calltools/campaigns/

    Returns ``[{id, uuid, name, active}]`` for the Lead Source dropdown.
    Query param ``active_only=true`` restricts to active campaigns.
    Query param ``refresh=true`` bypasses the cache.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not config.is_enabled():
            return Response(
                {"detail": "CallTools is not configured (CALLTOOLS_API_TOKEN)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        active_only = request.query_params.get("active_only") == "true"
        refresh = request.query_params.get("refresh") == "true"
        cache_key = f"{_CACHE_KEY}:{int(active_only)}"

        if not refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return Response(cached)

        try:
            options = campaigns.list_campaign_options(active_only=active_only)
        except client.CallToolsError as exc:
            logger.warning("CallTools campaigns fetch failed: %s", exc)
            return Response(
                {"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY
            )

        cache.set(cache_key, options, _CACHE_TTL)
        return Response(options)


class CallToolsAgentStatusView(views.APIView):
    """GET /api/agents/<code>/calltools/

    Returns the agent's live CallTools presence and active call:
        {app_user, logged_in, on_call, status, active_call}
    where status is on_call | online | offline | unknown.

    Pass ``?client_phone=<number>`` to also get ``active_call.matches_client``
    (last-10-digit comparison) so the extension can flag a match with the
    currently loaded client.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, code):
        if not config.is_enabled():
            return Response(
                {"detail": "CallTools is not configured (CALLTOOLS_API_TOKEN)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        agent = Agent.objects.filter(agent_code=code).first()
        if agent is None:
            return Response(
                {"detail": f"No agent with code {code}."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not agent.calltools_app_user:
            return Response(
                {
                    "app_user": None,
                    "status": "unknown",
                    "logged_in": False,
                    "on_call": False,
                    "active_call": None,
                    "detail": "Agent is not linked to a CallTools account.",
                }
            )

        client_phone = request.query_params.get("client_phone")
        snapshot = presence.agent_presence(
            agent.calltools_app_user, client_phone=client_phone
        )
        return Response(snapshot)

"""Agent authentication and validation views."""

import logging
from datetime import timedelta
from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import AccessToken
from .models import Agent
from .integrations.calltools import config as ct_config, presence as ct_presence

logger = logging.getLogger(__name__)


def _calltools_presence(agent):
    """Best-effort CallTools presence for an agent; never raises."""
    if not (ct_config.is_enabled() and agent.calltools_app_user):
        return None
    try:
        return ct_presence.agent_presence(agent.calltools_app_user)
    except Exception as exc:  # pragma: no cover - presence is non-critical
        logger.warning("CallTools presence lookup failed for %s: %s", agent.agent_code, exc)
        return None


class AgentLoginView(views.APIView):
    """
    POST /api/agents/login/
    
    Validate agent by code and return JWT token with 24-hour expiration.
    Payload: {"agent_code": "355"}
    """
    
    permission_classes = []
    authentication_classes = []
    
    def post(self, request):
        agent_code = request.data.get('agent_code', '').strip()
        
        if not agent_code:
            return Response(
                {"error": "Agent code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            agent = Agent.objects.get(agent_code=agent_code, status='Active')
        except Agent.DoesNotExist:
            return Response(
                {"error": "Invalid agent code or agent is inactive"},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        # Build an access token directly so we control the 24-hour lifetime
        # (RefreshToken.access_token would use the short default lifetime).
        access = AccessToken()
        access.set_exp(lifetime=timedelta(hours=24))
        access['agent_id'] = str(agent.id)
        access['agent_code'] = agent.agent_code
        access['agent_name'] = agent.name
        access['agent_group'] = agent.group

        return Response({
            "success": True,
            "agent": {
                "id": str(agent.id),
                "name": agent.name,
                "agent_code": agent.agent_code,
                "group": agent.group,
                "cbo": agent.cbo,
            },
            "calltools": _calltools_presence(agent),
            "access_token": str(access),
            "expires_in": 86400,  # 24 hours in seconds
            "expires_at": (timezone.now() + timedelta(hours=24)).isoformat(),
        })


class AgentValidateView(views.APIView):
    """
    GET /api/agents/validate/?code=355
    
    Quick validation to check if agent code exists and get agent info.
    Used for home screen validation before login.
    """
    
    permission_classes = []
    authentication_classes = []
    
    def get(self, request):
        agent_code = request.query_params.get('code', '').strip()
        
        if not agent_code:
            return Response(
                {"error": "Agent code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            agent = Agent.objects.get(agent_code=agent_code, status='Active')
            return Response({
                "valid": True,
                "agent": {
                    "id": str(agent.id),
                    "name": agent.name,
                    "agent_code": agent.agent_code,
                    "group": agent.group,
                }
            })
        except Agent.DoesNotExist:
            return Response({
                "valid": False,
                "error": "Invalid agent code or agent is inactive"
            }, status=status.HTTP_404_NOT_FOUND)

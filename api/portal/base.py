"""Shared base classes / helpers for portal views."""

from rest_framework.generics import GenericAPIView
from rest_framework.views import APIView

from ..models import Agent
from .pagination import PortalPagination
from .permissions import IsPortalAgent


class PortalAPIView(APIView):
    permission_classes = [IsPortalAgent]


class PortalGenericAPIView(GenericAPIView):
    permission_classes = [IsPortalAgent]
    pagination_class = PortalPagination


def current_agent(request):
    """Resolve the logged-in agent's DB row from the JWT principal (or None)."""
    agent_id = getattr(getattr(request, "user", None), "agent_id", None)
    if not agent_id:
        return None
    return Agent.objects.filter(pk=agent_id).first()

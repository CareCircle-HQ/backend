"""Portal API: on-demand Unite Us refresh for one member / case.

Backs the CRM "Refresh from Unite Us" button on the Cases tab. Pulls fresh case
+ authorization data through the same pipeline as the nightly daily pull, so an
authorization that has flipped to Accepted advances the enrollment and generates
deliveries immediately.
"""
import logging

from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..integrations.uniteus import config as uu_config
from ..models import Client, UniteUsCredential, UniteUsCredentialStatus
from .base import PortalAPIView, current_agent

logger = logging.getLogger(__name__)


class MemberUniteUsRefreshView(PortalAPIView):
    """POST /api/portal/members/<client_id>/refresh-uniteus/

    Body (optional): ``{"case_id": "<uuid>"}`` to refresh only that
    internal-service case; omit it to refresh the whole member (all cases +
    coverage + notes).

    Always returns HTTP 200 with a structured result so the UI can branch on
    ``ok`` / ``needs_reconnect`` without special-casing HTTP errors. The one
    exception is the integration being disabled server-side (503).
    """

    def post(self, request, client_id):
        if not uu_config.is_enabled():
            return Response(
                {
                    "ok": False,
                    "needs_reconnect": False,
                    "message": "The Unite Us integration is not enabled on this server.",
                },
                status=http.HTTP_503_SERVICE_UNAVAILABLE,
            )

        client = get_object_or_404(Client, pk=client_id)
        case_id = str(request.data.get("case_id") or "").strip() or None

        # Prefer the requesting agent's captured session, else the freshest active
        # provider credential (the daily cron pull uses every active cred anyway).
        # Defer the encrypted token columns so merely SELECTING a credential never
        # eagerly decrypts (a wrong/rotated FIELD_ENCRYPTION_KEY would otherwise
        # 500 here); decryption is attempted later where it's handled gracefully.
        agent = current_agent(request)
        active = (
            UniteUsCredential.objects.filter(status=UniteUsCredentialStatus.ACTIVE)
            .defer("access_token", "refresh_token")
            .order_by("-last_captured_at", "-updated_at")
        )
        cred = (active.filter(agent=agent).first() if agent else None) or active.first()
        if cred is None:
            return Response(
                {
                    "ok": False,
                    "needs_reconnect": True,
                    "message": (
                        "No active Unite Us session. Open Unite Us in the browser so "
                        "the extension can capture a session, then try again."
                    ),
                },
                status=http.HTTP_200_OK,
            )

        agent_code = (
            getattr(request.user, "agent_code", "")
            or (str(agent.pk) if agent else "?")
        )

        # Imported lazily to keep the whole import pipeline off the hot path.
        from ..services.uniteus_import import refresh_from_uniteus

        result = refresh_from_uniteus(
            str(client.pk),
            case_id=case_id,
            provider_id=cred.provider_id,
            triggered_by=f"portal:agent:{agent_code}",
        )
        return Response(result, status=http.HTTP_200_OK)

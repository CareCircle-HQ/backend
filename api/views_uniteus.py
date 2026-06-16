"""Unite Us credential capture endpoint.

The Chrome extension observes the Unite Us auth flow (see
extension/content/uw_netcapture.js) and POSTs the captured session here so the
backend can refresh it autonomously for the daily pull. Tokens are stored
encrypted (api.fields.EncryptedTextField); the daily job refreshes them via the
refresh-token grant (api.integrations.uniteus.client).
"""

import logging
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status, views
from rest_framework.response import Response

from .models import Agent, UniteUsCredential, UniteUsCredentialStatus

logger = logging.getLogger(__name__)


class UniteUsCredentialCaptureView(views.APIView):
    """POST /api/uniteus/credentials/

    Upsert the captured Unite Us credentials, keyed by (provider_id, employee_id).
    Payload: {
        "provider_id": "...",          # required (x-provider-id)
        "employee_id": "...",          # optional (x-employee-id)
        "access_token": "...",
        "refresh_token": "...",
        "expires_at": "ISO-8601",      # or
        "expires_in": 3600,            # seconds
        "scope": "...",
        "token_type": "Bearer"
    }
    """

    def post(self, request):
        data = request.data
        provider_id = str(data.get("provider_id") or "").strip()
        if not provider_id:
            return Response(
                {"error": "provider_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        employee_id = str(data.get("employee_id") or "").strip()

        # Resolve access-token expiry from either expires_at (ISO) or expires_in.
        expires_at = None
        raw_expires_at = data.get("expires_at")
        if raw_expires_at:
            expires_at = parse_datetime(str(raw_expires_at))
        if expires_at is None and data.get("expires_in"):
            try:
                expires_at = timezone.now() + timedelta(seconds=int(data["expires_in"]))
            except (TypeError, ValueError):
                expires_at = None

        defaults = {
            "access_token": data.get("access_token") or "",
            "refresh_token": data.get("refresh_token") or "",
            "access_expires_at": expires_at,
            "scope": data.get("scope") or "",
            "token_type": data.get("token_type") or "Bearer",
            "status": UniteUsCredentialStatus.ACTIVE,
            "last_captured_at": timezone.now(),
        }

        # Link the pushing agent if the request is agent-authenticated.
        agent_id = getattr(request.user, "agent_id", None)
        if agent_id:
            defaults["agent"] = Agent.objects.filter(pk=agent_id).first()

        cred, created = UniteUsCredential.objects.update_or_create(
            provider_id=provider_id, employee_id=employee_id, defaults=defaults
        )

        return Response(
            {
                "id": cred.pk,
                "provider_id": cred.provider_id,
                "employee_id": cred.employee_id,
                "status": cred.status,
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

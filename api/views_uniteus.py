"""Unite Us credential capture endpoint.

The Chrome extension observes the Unite Us auth flow (see
extension/content/uw_netcapture.js) and POSTs the captured session here so the
backend can refresh it autonomously for the daily pull. Tokens are stored
encrypted (api.fields.EncryptedTextField); the daily job refreshes them via the
refresh-token grant (api.integrations.uniteus.client).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status, views
from rest_framework.permissions import IsAuthenticated
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

        # NB: do NOT use update_or_create here -- a matching row may already
        # exist whose token was encrypted with a DIFFERENT FIELD_ENCRYPTION_KEY
        # (e.g. a prod-copy DB used in local dev). update_or_create's internal
        # get() would materialize that row and try to DECRYPT its token columns,
        # raising ImproperlyConfigured (500) before we ever write. Find the pk
        # WITHOUT selecting the encrypted columns, then .update() (which only
        # ENCRYPTS on write, never decrypts) or create fresh. Mirrors the
        # set_uniteus_credential management command.
        existing_pk = (
            UniteUsCredential.objects.filter(
                provider_id=provider_id, employee_id=employee_id
            )
            .values_list("pk", flat=True)
            .first()
        )
        if existing_pk:
            UniteUsCredential.objects.filter(pk=existing_pk).update(**defaults)
            pk, created = existing_pk, False
        else:
            obj = UniteUsCredential.objects.create(
                provider_id=provider_id, employee_id=employee_id, **defaults
            )
            pk, created = obj.pk, True

        return Response(
            {
                "id": pk,
                "provider_id": provider_id,
                "employee_id": employee_id,
                "status": UniteUsCredentialStatus.ACTIVE,
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class UniteUsRunUpdateView(views.APIView):
    """POST /api/uniteus/run-update/

    On-demand trigger for the Unite Us data pull, invoked from the extension's
    "Sync Now" button. Runs the pull **synchronously** scoped to the requesting
    agent's active provider credential and returns the resulting ImportRun
    summary so the panel can show what changed.

    Body (optional):
        {"client_id": "<unite-us person uuid>"}  # pull just this one client
    With no client_id the agent's whole client set is refreshed (cron-like).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        # On-demand ("Sync Now") pulls run the full Unite Us import inline and
        # can pin a gunicorn worker thread for minutes; a few concurrent clicks
        # exhaust every worker and take the whole site down (the ALB health
        # check then times out and flaps the target Unhealthy -> 504s). Disabled
        # by default: the nightly `manage.py daily_pull` cron keeps data fresh.
        # Set UNITEUS_ONDEMAND_SYNC_ENABLED=true to re-enable (ideally only once
        # this runs off the request path, e.g. via Celery).
        if not getattr(settings, "UNITEUS_ONDEMAND_SYNC_ENABLED", False):
            return Response(
                {
                    "error": (
                        "On-demand Unite Us sync is disabled. Data refreshes "
                        "automatically in the nightly pull."
                    ),
                    "disabled": True,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        agent_id = getattr(request.user, "agent_id", None)
        if not agent_id:
            return Response(
                {"error": "Agent authentication required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Prefer a credential captured by the requesting agent, but fall back to
        # any active credential. The captured credential is the org's shared
        # Unite Us provider session, and the daily cron pull already uses every
        # active credential regardless of agent. Agents commonly have several
        # Agent records (duplicate/again-issued codes for the same person), so
        # the captured session may be linked to a sibling record — don't 409 in
        # that case when a usable provider session exists.
        active = UniteUsCredential.objects.filter(
            status=UniteUsCredentialStatus.ACTIVE
        ).order_by("-updated_at")
        cred = active.filter(agent_id=agent_id).first() or active.first()
        if cred is None:
            return Response(
                {
                    "error": (
                        "No active Unite Us credential for this agent. Log in to "
                        "Unite Us in the browser so the extension can capture a "
                        "session, then try again."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        client_id = str(request.data.get("client_id") or "").strip()
        client_ids = [client_id] if client_id else None
        agent_code = getattr(request.user, "agent_code", "") or str(agent_id)

        # Imported here (not at module load) to keep this view light and avoid
        # pulling the whole import pipeline on every request import cycle.
        from .services.uniteus_import import run_daily_pull

        run = run_daily_pull(
            triggered_by=f"extension:agent:{agent_code}",
            provider_id=cred.provider_id,
            client_ids=client_ids,
        )

        return Response(
            {
                "import_run_id": run.pk,
                "status": run.status,
                "created": run.created_count,
                "updated": run.updated_count,
                "skipped": run.skipped_count,
                "errors": run.error_count,
                "stats": run.stats or {},
                "error_log": run.error_log or "",
                "scope": {
                    "client_id": client_id or None,
                    "provider_id": cred.provider_id,
                },
            },
            status=status.HTTP_200_OK,
        )

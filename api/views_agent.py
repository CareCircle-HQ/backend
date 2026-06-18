"""Agent authentication and validation views."""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import AccessToken

from .integrations.calltools import config as ct_config, presence as ct_presence
from .integrations.mailgun import MailgunError, send_email
from .models import Agent, AgentLoginCode

logger = logging.getLogger(__name__)


def _twofa_email_bodies(agent, code, ttl_minutes):
    """(subject, text, html) for the 2FA code email."""
    name = (agent.first_name or agent.name or "there").split(" ")[0]
    subject = f"Your CareCircle login code: {code}"
    text = (
        f"Hi {name},\n\n"
        f"Your CareCircle extension login code is: {code}\n\n"
        f"This code expires in {ttl_minutes} minutes. If you didn't request it, "
        f"you can ignore this email.\n"
    )
    html = (
        f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        f"color:#1e293b\">"
        f"<p>Hi {name},</p>"
        f"<p>Your CareCircle extension login code is:</p>"
        f"<p style=\"font-size:28px;font-weight:700;letter-spacing:4px;"
        f"color:#0F766E\">{code}</p>"
        f"<p style=\"color:#64748b\">This code expires in {ttl_minutes} minutes. "
        f"If you didn't request it, you can ignore this email.</p>"
        f"</div>"
    )
    return subject, text, html


def _calltools_presence(agent):
    """Best-effort CallTools presence for an agent; never raises."""
    if not (ct_config.is_enabled() and agent.calltools_app_user):
        return None
    try:
        return ct_presence.agent_presence(agent.calltools_app_user)
    except Exception as exc:  # pragma: no cover - presence is non-critical
        logger.warning("CallTools presence lookup failed for %s: %s", agent.agent_code, exc)
        return None


def _agent_login_response(agent):
    """Build the standard successful-login payload (JWT + agent info).

    Shared by the agent-code login and the email/2FA verify flow so both return
    an identical shape. ``agent_code`` may be null for agents without a dialer
    extension; ``calltools_enabled`` tells the client whether to surface the
    CallTools dialer features (only when the agent has a code).
    """
    # Build an access token directly so we control the 24-hour lifetime
    # (RefreshToken.access_token would use the short default lifetime).
    access = AccessToken()
    access.set_exp(lifetime=timedelta(hours=24))
    access["agent_id"] = str(agent.id)
    access["agent_code"] = agent.agent_code
    access["agent_name"] = agent.name
    access["agent_group"] = agent.group

    has_code = bool(agent.agent_code)
    return {
        "success": True,
        "agent": {
            "id": str(agent.id),
            "name": agent.name,
            "email": agent.email,
            "agent_code": agent.agent_code,
            "group": agent.group,
            "cbo": agent.cbo,
        },
        # The dialer is only usable when the agent has a CallTools extension
        # (agent_code). The client should hide/disable CallTools UI otherwise.
        "calltools_enabled": has_code,
        "calltools": _calltools_presence(agent) if has_code else None,
        "access_token": str(access),
        "expires_in": 86400,  # 24 hours in seconds
        "expires_at": (timezone.now() + timedelta(hours=24)).isoformat(),
    }


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

        return Response(_agent_login_response(agent))


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


class AgentRequestCodeView(views.APIView):
    """POST /api/agents/request-code/

    Step 1 of email + 2FA login: email a short-lived one-time code to an agent's
    company email. Body: {"email": "agent@carecirclecs.com"}.

    The email is matched (case-insensitively) to an ACTIVE agent. To avoid
    leaking which company emails are registered, the response is the same
    whether or not a match is found — a code is only generated and sent when an
    active agent matches. A per-email cooldown throttles resends.

    Verification of the code (and minting the JWT) is a separate endpoint.
    """

    permission_classes = []
    authentication_classes = []

    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        if not email:
            return Response(
                {"error": "Email is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ttl_seconds = getattr(settings, "AGENT_2FA_CODE_TTL_SECONDS", 600)
        cooldown = getattr(settings, "AGENT_2FA_RESEND_COOLDOWN_SECONDS", 30)
        # Uniform response so callers can't enumerate registered emails.
        generic = Response(
            {
                "sent": True,
                "message": (
                    "If that email belongs to an active agent, a login code has "
                    "been sent."
                ),
                "expires_in": ttl_seconds,
            }
        )

        agent = Agent.objects.filter(email__iexact=email, status="Active").first()
        if agent is None:
            logger.info("2FA code requested for unknown/inactive email: %s", email)
            return generic

        # Throttle: skip if a still-valid code was issued within the cooldown.
        recent = (
            AgentLoginCode.objects.filter(
                email=email,
                consumed_at__isnull=True,
                created_at__gte=timezone.now() - timedelta(seconds=cooldown),
            )
            .order_by("-created_at")
            .first()
        )
        if recent is not None:
            logger.info("2FA resend throttled for %s", email)
            return generic

        obj, code = AgentLoginCode.issue(email, agent=agent, ttl_seconds=ttl_seconds)
        subject, text, html = _twofa_email_bodies(agent, code, ttl_seconds // 60)
        try:
            send_email(to=f"{agent.name} <{email}>", subject=subject, text=text, html=html)
        except MailgunError as exc:
            # Drop the unsendable code so it can't be brute-forced later.
            obj.delete()
            logger.exception("Mailgun send failed for %s: %s", email, exc)
            return Response(
                {"error": "Could not send the login code right now. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return generic


class AgentVerifyCodeView(views.APIView):
    """POST /api/agents/verify-code/

    Step 2 of email + 2FA login: verify the emailed code and, on success, mint
    the agent JWT. Body: {"email": "...", "code": "123456"}.

    The newest unconsumed, unexpired code for the email is checked. A wrong code
    increments an attempt counter; once it exceeds AGENT_2FA_MAX_ATTEMPTS the
    code is burned (consumed) so it can't be brute-forced. A correct code is
    consumed (single-use) and the standard login payload is returned — including
    ``agent_code`` (may be null) and ``calltools_enabled``.
    """

    permission_classes = []
    authentication_classes = []

    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        code = str(request.data.get("code") or "").strip()
        if not email or not code:
            return Response(
                {"error": "Email and code are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_attempts = getattr(settings, "AGENT_2FA_MAX_ATTEMPTS", 5)
        invalid = Response(
            {"error": "Invalid or expired code."},
            status=status.HTTP_400_BAD_REQUEST,
        )

        login_code = (
            AgentLoginCode.objects.filter(email=email, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if login_code is None or login_code.is_expired:
            return invalid

        if login_code.attempts >= max_attempts:
            # Too many tries: burn it so further guesses are pointless.
            login_code.consumed_at = timezone.now()
            login_code.save(update_fields=["consumed_at"])
            return Response(
                {"error": "Too many attempts. Request a new code."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if not login_code.check_code(code):
            login_code.attempts += 1
            login_code.save(update_fields=["attempts"])
            return invalid

        # Correct code: consume (single-use) and resolve the agent.
        login_code.consumed_at = timezone.now()
        login_code.save(update_fields=["consumed_at"])

        agent = login_code.agent
        if agent is None:
            agent = Agent.objects.filter(email__iexact=email, status="Active").first()
        if agent is None or agent.status != "Active":
            return Response(
                {"error": "Agent is no longer active."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(_agent_login_response(agent))

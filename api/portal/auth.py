"""Portal email + 2FA login.

Reuses the same one-time-code machinery as the extension agent login
(``AgentLoginCode`` + the shared email/JWT helpers in ``api.views_agent``) but
gates on the agent's group: only Verifiers / Management / CS may obtain a
portal session. Screeners are rejected.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response

from ..integrations.mailgun import MailgunError, send_email
from ..models import Agent, AgentLoginCode
from ..views_agent import _agent_login_response, _twofa_email_bodies
from .permissions import is_portal_group

logger = logging.getLogger(__name__)


class PortalRequestCodeView(views.APIView):
    """POST /api/portal/auth/request-code/  body: {"email": "..."}

    Emails a one-time login code. Uniform response regardless of whether the
    email matches an active, portal-eligible agent (no account enumeration). A
    code is only issued for an ACTIVE agent in an allowed group.
    """

    permission_classes = []
    authentication_classes = []

    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        if not email:
            return Response(
                {"error": "Email is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        ttl_seconds = getattr(settings, "AGENT_2FA_CODE_TTL_SECONDS", 600)
        cooldown = getattr(settings, "AGENT_2FA_RESEND_COOLDOWN_SECONDS", 30)
        generic = Response(
            {
                "sent": True,
                "message": (
                    "If that email belongs to a support-portal agent, a login "
                    "code has been sent."
                ),
                "expires_in": ttl_seconds,
            }
        )

        agent = Agent.objects.filter(email__iexact=email, status="Active").first()
        # Don't issue a code for unknown emails or for agents (e.g. Screeners)
        # who aren't allowed into the portal — but keep the response uniform.
        if agent is None or not is_portal_group(agent.group):
            logger.info("Portal 2FA requested for ineligible email: %s", email)
            return generic

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
            logger.info("Portal 2FA resend throttled for %s", email)
            return generic

        obj, code = AgentLoginCode.issue(email, agent=agent, ttl_seconds=ttl_seconds)
        subject, text, html = _twofa_email_bodies(agent, code, ttl_seconds // 60)
        try:
            send_email(to=f"{agent.name} <{email}>", subject=subject, text=text, html=html)
        except MailgunError as exc:
            obj.delete()
            logger.exception("Mailgun send failed for %s: %s", email, exc)
            return Response(
                {"error": "Could not send the login code right now. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return generic


class PortalVerifyCodeView(views.APIView):
    """POST /api/portal/auth/verify-code/  body: {"email": "...", "code": "..."}

    Verifies the code and mints a 24h agent JWT — but only for agents in an
    allowed portal group. Screeners (or any other group) get 403.
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
            {"error": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST
        )

        login_code = (
            AgentLoginCode.objects.filter(email=email, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if login_code is None or login_code.is_expired:
            return invalid

        if login_code.attempts >= max_attempts:
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
        if not is_portal_group(agent.group):
            return Response(
                {"error": "Your agent group does not have access to the support portal."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(_agent_login_response(agent))

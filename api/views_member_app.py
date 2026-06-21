"""Benefully member mobile-app authentication.

Household members sign in to the mobile app with their chosen mobile number
(``HouseholdMember.mobile_app_username``) and a one-time 2FA code.

Delivery will eventually be SMS via Twilio. Until that's wired up, the code is
**emailed to an operator inbox** (``MEMBER_APP_2FA_BACKUP_EMAIL``, default
alexis@carecirclecs.com) so the flow can be exercised and the data captured.
The TODO below marks exactly where the Twilio call should slot in.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response

from .integrations.mailgun import MailgunError, send_email
from .models import HouseholdMember, HouseholdMemberLoginCode

logger = logging.getLogger(__name__)

# Where 2FA codes are emailed until Twilio SMS is wired up.
BACKUP_2FA_EMAIL = getattr(
    settings, "MEMBER_APP_2FA_BACKUP_EMAIL", "alexis@carecirclecs.com"
)


def _normalize_mobile(raw):
    """Trim and collapse whitespace; keep the member-entered format otherwise."""
    return " ".join(str(raw or "").split()).strip()


def _digits(raw):
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _deliver_2fa_backup(mobile_number, code, member, ttl_minutes):
    """Email the 2FA code + context to the operator inbox.

    TODO(twilio): replace this with an SMS to ``mobile_number`` once the Twilio
    integration is configured; keep the email as a fallback only.
    """
    if member is not None:
        client = member.client
        who = f"{client.first_name} {client.last_name}".strip() or str(client.client_id)
        member_line = (
            f"Matched household member: {who} "
            f"(client {client.client_id}, household {member.household_id})"
        )
    else:
        member_line = "No household member is registered with this mobile number yet."

    subject = f"[Member App 2FA] {code} for {mobile_number}"
    text = (
        f"A Benefully member-app login code was requested.\n\n"
        f"Mobile number: {mobile_number}\n"
        f"2FA code: {code}\n"
        f"Expires in: {ttl_minutes} minutes\n\n"
        f"{member_line}\n\n"
        f"(SMS delivery via Twilio is not configured yet — this email is the "
        f"interim backup so the code can be relayed/verified.)\n"
    )
    send_email(to=BACKUP_2FA_EMAIL, subject=subject, text=text)


class MemberAppRequestCodeView(views.APIView):
    """POST /api/member-app/request-code/  body: {"mobile_number": "..."}

    Issues a one-time 2FA code for a household member's mobile-app login and
    delivers it (currently by emailing the operator inbox; SMS via Twilio later).

    The response is uniform regardless of whether a member is registered with
    the number, to avoid leaking which numbers exist.
    """

    permission_classes = []
    authentication_classes = []

    def post(self, request):
        mobile_number = _normalize_mobile(
            request.data.get("mobile_number") or request.data.get("username")
        )
        if len(_digits(mobile_number)) < 10:
            return Response(
                {"error": "A valid mobile number is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ttl_seconds = getattr(settings, "AGENT_2FA_CODE_TTL_SECONDS", 600)
        cooldown = getattr(settings, "AGENT_2FA_RESEND_COOLDOWN_SECONDS", 30)
        generic = Response(
            {
                "sent": True,
                "message": "If that number is eligible, a login code has been sent.",
                "expires_in": ttl_seconds,
            }
        )

        # Throttle resends per mobile number.
        recent = (
            HouseholdMemberLoginCode.objects.filter(
                mobile_number=mobile_number,
                consumed_at__isnull=True,
                created_at__gte=timezone.now() - timedelta(seconds=cooldown),
            )
            .order_by("-created_at")
            .first()
        )
        if recent is not None:
            logger.info("Member-app 2FA resend throttled for %s", mobile_number)
            return generic

        member = HouseholdMember.objects.filter(
            mobile_app_username=mobile_number
        ).select_related("client").first()

        obj, code = HouseholdMemberLoginCode.issue(
            mobile_number, member=member, ttl_seconds=ttl_seconds
        )
        try:
            _deliver_2fa_backup(mobile_number, code, member, ttl_seconds // 60)
        except MailgunError as exc:
            # Local dev fallback: don't fail the flow when email isn't configured.
            if settings.DEBUG:
                logger.warning(
                    "\n[DEV MEMBER 2FA] delivery unavailable; code for %s: %s "
                    "(valid %d min)\n",
                    mobile_number,
                    code,
                    ttl_seconds // 60,
                )
                return generic
            obj.delete()
            logger.exception("Member-app 2FA delivery failed for %s: %s", mobile_number, exc)
            return Response(
                {"error": "Could not send the login code right now. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return generic

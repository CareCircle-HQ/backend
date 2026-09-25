"""Member mobile-app views.

Every query is scoped from ``request.user`` -- the household member, their household or
their client, depending on what the data belongs to. A ``client_id`` never appears in a
URL on this surface.

⚠ WHAT A MEMBER SEES IS NOT WHAT AN AGENT SEES. The portal's member serializers carry
agent-facing judgements -- hold reasons, warning codes, authorization states, internal
notes -- and a member reading those would be reading what we say about them internally.
Nothing here reuses a portal serializer without stripping it deliberately.
"""
import logging

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import HouseholdMember, MemberAccessToken
from .auth import (
    IsMember, MemberAuthentication, authenticate_member, issue_token,
    normalize_username,
)

logger = logging.getLogger(__name__)


def error(code, detail, status_code=http.HTTP_400_BAD_REQUEST, **extra):
    """Machine-readable error body: the app branches on ``code``, not on prose."""
    body = {"error": code, "detail": detail}
    body.update(extra)
    return Response(body, status=status_code)


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", ""))


class MemberAPIView(APIView):
    """Base: member-only auth, and nothing inherited from the CRM's defaults.

    ⚠ IT ALSO ENFORCES THE FORCED PASSWORD CHANGE, the same way the vendor surface does.
    An agent issued the password and read it to the member, so until it is replaced the
    credential is known to two people. Every endpoint returns 403
    ``password_change_required`` until then -- a gate, not a screen the app is trusted
    to show.
    """

    authentication_classes = [MemberAuthentication]
    permission_classes = [IsMember]
    #: True on a view that must work BEFORE the password has been replaced.
    ALLOW_PENDING_PASSWORD = False

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self.ALLOW_PENDING_PASSWORD:
            return
        principal = getattr(request, "user", None)
        if getattr(principal, "must_change_password", False):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied({
                "error": "password_change_required",
                "detail": "Choose your own password before continuing.",
            })


class MemberLoginView(APIView):
    """POST /v1/auth/login/ -- mobile number + password.

    One of TWO ways in; the other is the OTP flow the app can offer instead. Both end
    at the same token, so nothing downstream cares which was used.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "anon"

    def post(self, request):
        data = request.data or {}
        username = data.get("username") or data.get("mobile_number") or ""
        password = data.get("password") or ""
        if not username or not password:
            return error(
                "missing_credentials", "Mobile number and password are required.",
            )
        member = authenticate_member(username, password)
        if member is None:
            # ONE message for every failure mode. A different answer for "no such
            # member" would let anyone test which phone numbers belong to Medicaid
            # members -- which is a disclosure in itself.
            return error(
                "invalid_credentials", "Mobile number or password is incorrect.",
                http.HTTP_401_UNAUTHORIZED,
            )
        raw, token = issue_token(
            member,
            ip=_client_ip(request),
            device_label=(data.get("device_label") or "").strip(),
        )
        client = member.client
        return Response({
            "access_token": raw,
            "expires_at": token.expires_at,
            "member": {
                "id": str(member.id),
                "first_name": getattr(client, "first_name", "") if client else "",
                "is_primary": member.is_primary,
                # So the app can go straight to the change screen rather than
                # discovering the 403 by being refused.
                "must_change_password": member.mobile_app_must_change_password,
            },
        })


class MemberLogoutView(MemberAPIView):
    """POST /v1/auth/logout/ -- revoke the presented token.

    Allowed before the forced change: a member who does not want to continue must be
    able to leave.
    """

    ALLOW_PENDING_PASSWORD = True

    def post(self, request):
        MemberAccessToken.objects.filter(pk=request.user.token.pk).update(
            revoked_at=timezone.now(),
        )
        return Response({"ok": True})


class MemberMeView(MemberAPIView):
    """GET /v1/me/ -- who am I.

    Reachable before the password change, or the app cannot render "choose your
    password, NAME".

    ⚠ DELIBERATELY THIN. No Medicaid ID, no plan, no case state, no warnings. This is
    the identity the app needs to address the member, not a profile -- each of those
    belongs to a screen that can justify it.
    """

    ALLOW_PENDING_PASSWORD = True

    def get(self, request):
        member = request.user.household_member
        client = request.user.client
        return Response({
            "member": {
                "id": str(member.id),
                "first_name": getattr(client, "first_name", "") if client else "",
                "last_name": getattr(client, "last_name", "") if client else "",
                "is_primary": member.is_primary,
                "must_change_password": member.mobile_app_must_change_password,
            },
            "household": {"id": str(member.household_id)},
            "token_expires_at": request.user.token.expires_at,
        })


class MemberPasswordView(MemberAPIView):
    """POST /v1/me/password/ -- the member replaces their own password.

    ⚠ ALLOWED WHILE the change is pending, and it has to be: every other endpoint 403s
    until the password changes, and changing it is a call.

    The CURRENT password is required even so. An agent issued it and read it out, so
    somebody else knows it; asking again means a stolen token alone cannot lock the
    real member out of their own benefits.
    """

    ALLOW_PENDING_PASSWORD = True
    MIN_LENGTH = 8

    def post(self, request):
        data = request.data or {}
        current = data.get("current_password") or ""
        # ⚠ NEVER STRIP A PASSWORD ON THE WAY IN. A leading or trailing space is a
        # legitimate character, and trimming it stores something different from what
        # was typed. Trim only to DECIDE whether anything was supplied.
        _raw = data.get("new_password") or ""
        new = _raw if _raw.strip() else ""
        member = request.user.household_member

        if not check_password(current, member.mobile_app_password or ""):
            return error(
                "wrong_password", "That is not your current password.",
                http.HTTP_401_UNAUTHORIZED,
            )
        if len(new) < self.MIN_LENGTH:
            return error(
                "password_too_short",
                f"Choose at least {self.MIN_LENGTH} characters.",
            )
        if check_password(new, member.mobile_app_password or ""):
            return error(
                "password_unchanged", "Choose a password you have not used here.",
            )

        HouseholdMember.objects.filter(pk=member.pk).update(
            mobile_app_password=make_password(new),
            mobile_app_must_change_password=False,
        )
        # ⚠ EVERY OTHER SESSION IS REVOKED. A member changing their password after
        # someone else has seen it expects that to end the other person's access, and
        # the forced first change is exactly that situation.
        MemberAccessToken.objects.filter(
            household_member=member, revoked_at__isnull=True,
        ).exclude(pk=request.user.token.pk).update(revoked_at=timezone.now())
        logger.info("member %s changed their app password", member.id)
        return Response({"ok": True, "must_change_password": False})

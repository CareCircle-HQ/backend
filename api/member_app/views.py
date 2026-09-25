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

from ..models import (
    HouseholdMember, HouseholdMemberLoginCode, MemberAccessToken,
)
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


class MemberVerifyCodeView(APIView):
    """POST /v1/auth/verify-code/ -- exchange a one-time code for a session.

    THE SECOND WAY IN. A member who has forgotten their password asks for a code
    (``/api/member-app/request-code/`` on the CRM host, which predates this surface)
    and exchanges it here. Both paths end at the same :class:`MemberAccessToken`, so
    nothing downstream cares which was used.

    ⚠ A CODE LOGIN DOES NOT CLEAR ``must_change_password``. A member who never set
    their own password still has an agent-issued one, and letting the OTP route around
    that would turn the forced change into a suggestion.

    ⚠ AND IT DOES NOT SET ONE EITHER. A member who signs in by code with no password at
    all is refused: the CRM's Mobile App tab is the only thing that issues credentials,
    and a self-service path into an unprovisioned account would make the primary-only
    policy unenforceable.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "anon"

    MAX_ATTEMPTS = 5

    def post(self, request):
        from .auth import normalize_username

        data = request.data or {}
        digits = normalize_username(
            data.get("username") or data.get("mobile_number") or "",
        )
        code = str(data.get("code") or "").strip()
        if not digits or not code:
            return error("missing_code", "Mobile number and code are required.")

        # Uniform failure for every reason -- expired, wrong, already used, no such
        # member. A specific answer would tell an attacker which numbers exist and
        # which codes are live.
        generic = error(
            "invalid_code", "That code is not valid. Request a new one.",
            http.HTTP_401_UNAUTHORIZED,
        )

        record = (
            HouseholdMemberLoginCode.objects
            .filter(mobile_number=digits, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if record is None or record.is_expired or record.is_consumed:
            return generic
        if record.attempts >= self.MAX_ATTEMPTS:
            # Burn it rather than leave a guessable code alive.
            HouseholdMemberLoginCode.objects.filter(pk=record.pk).update(
                consumed_at=timezone.now(),
            )
            return generic
        if not record.check_code(code):
            HouseholdMemberLoginCode.objects.filter(pk=record.pk).update(
                attempts=record.attempts + 1,
            )
            return generic

        member = record.member or (
            HouseholdMember.objects
            .select_related("client", "household")
            .filter(mobile_app_username=digits)
            .first()
        )
        # ⚠ NO PASSWORD MEANS NO ACCOUNT, even with a valid code. 7,235 household
        # members carry a username from an old import; a code login must not become a
        # back door into those.
        if member is None or not member.mobile_app_enabled:
            return generic

        HouseholdMemberLoginCode.objects.filter(pk=record.pk).update(
            consumed_at=timezone.now(), member=member,
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
                "must_change_password": member.mobile_app_must_change_password,
            },
        })


# ── home ─────────────────────────────────────────────────────────────────────

class MemberDashboardView(MemberAPIView):
    """GET /v1/me/dashboard/ -- everything the Home screen shows.

    ONE request, not four. The home screen is the first thing a member sees, often on
    a phone with poor signal, and four round trips means four chances to show a
    spinner. The pieces are small and always shown together.

    ⚠ WHAT IS DELIBERATELY ABSENT: no case state, no hold reason, no warning codes, no
    Medicaid ID, no plan external ids. Those are agent-facing judgements, and a member
    reading them would be reading what we say about them internally.
    """

    def get(self, request):
        from django.utils import timezone

        from ..models import DeliveryOrder, Insurance

        member = request.user.household_member
        client = request.user.client
        today = timezone.localdate()

        first_name = (getattr(client, "first_name", "") or "").strip()

        # ── coverage ──────────────────────────────────────────────────────
        coverage = {"status": "unknown", "plan_name": "", "valid_until": None}
        if client is not None:
            ins = (
                Insurance.objects
                .filter(client=client)
                .order_by("-is_primary", "-enrolled_at")
                .first()
            )
            if ins is not None:
                coverage["plan_name"] = (ins.plan_name or ins.plan_type or "").strip()
                coverage["status"] = (
                    "active" if (ins.status or "").lower() == "active"
                    else "inactive"
                )
                # ⚠ 9999-12-31 IS A SENTINEL, NOT A DATE, and it is 47% of all
                # insurance rows. "Valid until: Dec 31, 9999" is nonsense on a
                # member's phone; an open-ended coverage has no end date to show, so
                # this returns null and the screen says "ongoing" instead.
                if ins.expired_at and ins.expired_at.year < 9999:
                    coverage["valid_until"] = ins.expired_at.date()

        # ── the next delivery ─────────────────────────────────────────────
        # ⚠ SCOPED TO THE CLIENT, not the household. DeliveryOrder.member is a
        # Client, and meals are cooked to ONE person's dietary profile -- showing a
        # household's deliveries would show a member food they must not eat.
        next_delivery = None
        if client is not None:
            row = (
                DeliveryOrder.objects
                .filter(member=client, expected_delivery_date__gte=today)
                .exclude(status__in=["cancelled", "canceled"])
                .order_by("expected_delivery_date")
                .first()
            )
            if row is not None:
                next_delivery = {
                    "date": row.expected_delivery_date,
                    "quantity": row.quantity,
                    "status": row.status,
                    "is_today": row.expected_delivery_date == today,
                }

        return Response({
            "member": {
                "id": str(member.id),
                # Title-cased: the CRM stores "JAMES BETHEA" in caps for matching, and
                # "Good morning, JAMES" reads as shouting at the person.
                "first_name": first_name.title(),
            },
            "coverage": coverage,
            "next_delivery": next_delivery,
        })

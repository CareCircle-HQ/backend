"""Member mobile-app authentication.

Separate from BOTH the agent auth and the vendor auth, and separate from the delivery
partner's machine credential. Three principals, three surfaces, and a member's phone is
the least trusted device of the lot.

⚠ WHY AN OPAQUE TOKEN AND NOT A JWT. The project's ``DEFAULT_AUTHENTICATION_CLASSES``
include JWT authenticators, so a member JWT signed with the shared key would
authenticate against the whole CRM. A random opaque token is meaningless to those
authenticators. This is the same reasoning as the vendor and partner surfaces; it is
restated here because it is the one thing that must not be "simplified" later.

TWO WAYS IN, by product decision:

  * PASSWORD -- mobile number as the username, password issued by an agent from the
    member profile's Mobile App tab and replaced at first sign-in.
  * OTP -- the pre-existing ``HouseholdMemberLoginCode`` flow, kept as the alternative
    for a member who has forgotten their password.

Both land on the same :class:`MemberAccessToken`, so everything downstream is identical
however they got in.
"""
import hashlib
import logging
import secrets

from django.contrib.auth.hashers import check_password
from django.utils import timezone
from rest_framework import authentication, exceptions, permissions

from ..models import HouseholdMember, MemberAccessToken

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "ccmt_"
# 14 days. Longer than the vendor's session because a member opens the app rarely --
# a benefits app is not a work tool, and asking someone to sign in every time is how
# they stop opening it. Shorter than "never" because the device is unmanaged.
TOKEN_TTL_SECONDS = 14 * 24 * 60 * 60


def generate_token():
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw):
    return hashlib.sha256((raw or "").encode()).hexdigest()


def normalize_username(raw):
    """A mobile number reduced to DIGITS ONLY for comparison.

    ⚠ MEMBERS DO NOT TYPE PHONE NUMBERS CONSISTENTLY. "(305) 781-3277",
    "305-781-3277" and "3057813277" are one number, and a member locked out of their
    benefits because they typed the brackets this time is the single most likely
    support call on this surface. Stored and compared as digits.
    """
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def authenticate_member(username, password):
    """Verify a mobile number + password. Returns the HouseholdMember or None.

    Deliberately indistinguishable between "no such member", "no app access" and
    "wrong password": this endpoint is on the public internet, and a different answer
    for each would let anyone test which phone numbers belong to Medicaid members.
    """
    digits = normalize_username(username)
    member = None
    if len(digits) >= 10:
        member = (
            HouseholdMember.objects
            .select_related("client", "household")
            .filter(mobile_app_username=digits)
            .first()
        )
    if member is None or not member.mobile_app_password:
        # Still hash something, so a missing member does not answer measurably faster
        # than a wrong password.
        check_password(password or "", "pbkdf2_sha256$1$x$x")
        return None
    if not check_password(password or "", member.mobile_app_password):
        return None
    return member


def issue_token(member, *, ip="", device_label=""):
    raw = generate_token()
    token = MemberAccessToken.objects.create(
        household_member=member,
        token_hash=hash_token(raw),
        expires_at=timezone.now() + timezone.timedelta(seconds=TOKEN_TTL_SECONDS),
        created_ip=(ip or "")[:64],
        device_label=(device_label or "")[:120],
    )
    HouseholdMember.objects.filter(pk=member.pk).update(
        mobile_app_last_login_at=timezone.now(),
    )
    return raw, token


class MemberPrincipal:
    """What ``request.user`` is on this surface.

    Carries the household member, and the household and client reachable from them --
    because WHICH of the three a given endpoint should scope to is a per-endpoint
    decision. Deliveries belong to the household; a dietary profile to the person;
    insurance to the client. A principal that exposed only one of them would push
    every endpoint into guessing.
    """

    is_authenticated = True

    def __init__(self, member, token):
        self.household_member = member
        self.household = member.household
        self.client = member.client
        self.token = token

    @property
    def must_change_password(self):
        return self.household_member.mobile_app_must_change_password

    def __str__(self):
        return f"member:{self.household_member_id_str}"

    @property
    def household_member_id_str(self):
        return str(self.household_member.id)


class MemberAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer ccmt_...``"""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].decode().lower() != self.keyword.lower():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Malformed Authorization header.")
        raw = header[1].decode()
        # Not a member token at all -- let another authenticator answer rather than
        # failing, so this class is inert on any other surface.
        if not raw.startswith(TOKEN_PREFIX):
            return None
        token = (
            MemberAccessToken.objects
            .select_related("household_member__client", "household_member__household")
            .filter(token_hash=hash_token(raw))
            .first()
        )
        if token is None or not token.is_valid:
            raise exceptions.AuthenticationFailed("Session expired. Sign in again.")
        MemberAccessToken.objects.filter(pk=token.pk).update(
            last_used_at=timezone.now(),
        )
        return MemberPrincipal(token.household_member, token), None


class IsMember(permissions.BasePermission):
    def has_permission(self, request, view):
        return isinstance(getattr(request, "user", None), MemberPrincipal)

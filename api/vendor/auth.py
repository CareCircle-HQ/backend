"""Authentication for the vendor API.

The principal is a PERSON at a vendor company, which is the one real difference
from the partner API: that one authenticates a machine with a client_id/secret,
this one authenticates a human with an email and password.

Credentials are exchanged for a short-lived OPAQUE bearer token. Opaque, not a
JWT: the project's default authenticators include JWT classes, so a vendor JWT
signed with the shared key would authenticate against the whole CRM.
"""

import hashlib
import logging
import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.utils import timezone

logger = logging.getLogger(__name__)
from rest_framework import authentication, exceptions, permissions

from ..models import VendorAccessToken, VendorUser

# Short by design. A vendor's phone is an unmanaged device that may hold member
# data offline, so a stolen token should stop working the same day -- and
# re-authenticating is cheap when the app can store the password in the OS
# keychain.
TOKEN_TTL_SECONDS = getattr(settings, "VENDOR_TOKEN_TTL_SECONDS", 12 * 3600)


def generate_token():
    """The raw bearer token. Returned to the caller ONCE and never stored."""
    return f"ccvt_{secrets.token_urlsafe(36)}"


def hash_token(raw):
    return hashlib.sha256((raw or "").encode()).hexdigest()


def client_ip(request):
    fwd = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
    return (fwd[0] or request.META.get("REMOTE_ADDR") or "").strip()


def authenticate_user(email, password):
    """Verify an email/password pair. Returns the VendorUser or None.

    Deliberately indistinguishable between "no such user", "wrong password" and
    "deactivated": this endpoint is on the public internet, and a different answer
    for each would let anyone enumerate which vendor staff exist.
    """
    user = VendorUser.objects.select_related("vendor").filter(
        email__iexact=(email or "").strip(),
    ).first()
    if user is None or not user.is_active or not user.vendor.is_active:
        # Still hash something, so a missing user does not answer measurably
        # faster than a wrong password.
        check_password(password or "", "pbkdf2_sha256$1$x$x")
        return None
    if not user.password or not check_password(password or "", user.password):
        return None
    return user


def issue_token(user, *, ip="", device_label=""):
    raw = generate_token()
    token = VendorAccessToken.objects.create(
        vendor_user=user,
        token_hash=hash_token(raw),
        expires_at=timezone.now() + timezone.timedelta(seconds=TOKEN_TTL_SECONDS),
        created_ip=(ip or "")[:64],
        device_label=(device_label or "")[:120],
    )
    return raw, token


class VendorPrincipal:
    """The authenticated vendor user.

    Not a Django user: it exists to satisfy DRF and to carry the two things every
    request needs -- the VENDOR every query must be scoped to, and the USER who
    did it.
    """

    def __init__(self, user, token):
        self.vendor_user = user
        self.token = token
        self.vendor = user.vendor
        self.vendor_id = user.vendor_id
        self.is_vendor_admin = user.is_admin
        # DRF's throttling keys off request.user.pk. Identifying the principal by
        # the USER rate-limits per person rather than per company, so one busy
        # assessor cannot throttle their colleagues.
        self.pk = user.pk
        self.id = user.pk

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    is_staff = False
    is_superuser = False
    is_anonymous = False

    def __str__(self):
        return f"{self.vendor_user.name} ({self.vendor.name})"


class VendorAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer <opaque access token>``."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None  # no credentials -> let permissions return 401
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Malformed Authorization header.")
        raw = header[1].decode()

        token = VendorAccessToken.objects.filter(
            token_hash=hash_token(raw),
        ).select_related("vendor_user", "vendor_user__vendor").first()
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid access token.")
        if not token.is_valid:
            raise exceptions.AuthenticationFailed("Access token expired or revoked.")
        # Re-checked on every request, not just at login: deactivating a vendor or
        # a member of their staff has to take effect immediately, not whenever
        # their token happens to expire.
        if not token.vendor_user.is_active or not token.vendor_user.vendor.is_active:
            raise exceptions.AuthenticationFailed("This account is deactivated.")

        VendorAccessToken.objects.filter(pk=token.pk).update(
            last_used_at=timezone.now(),
        )
        VendorUser.objects.filter(pk=token.vendor_user_id).update(
            last_login_at=timezone.now(),
        )

        principal = VendorPrincipal(token.vendor_user, token)
        return (principal, principal)

    def authenticate_header(self, request):
        return self.keyword


class IsVendorUser(permissions.BasePermission):
    """Authenticated as a vendor user.

    Belt-and-braces: the views already accept only vendor authentication. This
    catches a view that forgets to declare it.
    """

    message = "Vendor credentials are required."

    def has_permission(self, request, view):
        return isinstance(getattr(request, "user", None), VendorPrincipal)


class IsVendorAdmin(permissions.BasePermission):
    """The vendor's own administrator.

    Only they may add or remove their colleagues. The admin slot itself is
    CRM-provisioned and there is exactly one per vendor
    (``one_admin_user_per_vendor``), so a vendor cannot promote their way to a
    second one even through this API.
    """

    message = "Only your company administrator can do that."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return isinstance(user, VendorPrincipal) and user.is_vendor_admin


def send_temporary_password(vendor_user, raw_password, *, invited_by=""):
    """Email a new member their temporary password. Returns True when it went.

    ⚠ NEVER RAISES. An admin has already created the account by the time this runs, so
    a Mailgun outage must not undo it -- the endpoint reports ``emailed: false`` and
    still shows the password once, which is the fallback that existed before email did.
    Losing the account because the mail failed would be the wrong way round.

    The password is in the body because there is nothing to click: the vendor app has
    no magic-link flow, and a member who cannot see the password cannot get in. It is
    single-use in practice -- ``must_change_password`` forces a replacement at first
    login, server-side.
    """
    from ..integrations.mailgun import MailgunError, send_email

    vendor = vendor_user.vendor
    subject = f"Your {vendor.name} login for CareCircle"
    who = f" by {invited_by}" if invited_by else ""
    text = (
        f"Hello {vendor_user.name},\n\n"
        f"An account has been created for you{who} so you can work "
        f"{vendor.name}'s CareCircle assignments.\n\n"
        f"Email:    {vendor_user.email}\n"
        f"Password: {raw_password}\n\n"
        "You will be asked to choose your own password the first time you sign in. "
        "This one stops working at that point.\n\n"
        "If you were not expecting this, tell your administrator -- do not sign in."
    )
    html = (
        f"<p>Hello {vendor_user.name},</p>"
        f"<p>An account has been created for you{who} so you can work "
        f"<strong>{vendor.name}</strong>'s CareCircle assignments.</p>"
        f"<p><strong>Email:</strong> {vendor_user.email}<br>"
        f"<strong>Password:</strong> <code>{raw_password}</code></p>"
        "<p>You will be asked to choose your own password the first time you sign "
        "in. This one stops working at that point.</p>"
        "<p>If you were not expecting this, tell your administrator — do not sign "
        "in.</p>"
    )
    try:
        send_email(
            to=f"{vendor_user.name} <{vendor_user.email}>",
            subject=subject, text=text, html=html,
        )
        return True
    except (MailgunError, Exception):  # noqa: BLE001
        logger.exception(
            "vendor invite email failed for %s", vendor_user.vendor_user_id,
        )
        return False

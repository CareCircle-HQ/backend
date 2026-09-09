"""Authentication for the delivery-partner API.

Deliberately NOT added to ``DEFAULT_AUTHENTICATION_CLASSES``: only the partner
views declare it. A partner token presented to any CRM endpoint is therefore
unrecognised (401) rather than merely unauthorised, and an agent JWT is
unrecognised here. The isolation is structural, not a permission check.

Credentials are a ``client_id`` + ``client_secret`` exchanged for a short-lived
OPAQUE bearer token. Opaque, not a JWT: the project's default authenticators
include JWT classes, so a partner JWT signed with the shared key would
authenticate against the whole CRM.
"""

import hashlib
import ipaddress
import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from rest_framework import authentication, exceptions, permissions

from ..models import DeliveryCompanyApiClient, PartnerAccessToken

# How long an access token lives. Short, because it is cheap to re-exchange.
TOKEN_TTL_SECONDS = getattr(settings, "PARTNER_TOKEN_TTL_SECONDS", 3600)
# How long a rotated-away secret keeps working, so a vendor can redeploy.
ROTATION_GRACE_SECONDS = getattr(settings, "PARTNER_ROTATION_GRACE_SECONDS", 7 * 24 * 3600)


# --- credential helpers ----------------------------------------------------
def generate_client_id(company_name):
    """Public, identifiable client id, e.g. ``ccpod_qari_7f3a91c2``. The prefix
    makes the credential recognisable in logs and to secret scanners."""
    slug = "".join(c for c in (company_name or "").lower() if c.isalnum())[:12] or "partner"
    return f"ccpod_{slug}_{secrets.token_hex(4)}"


def generate_secret():
    """The raw client secret. Returned to the caller ONCE and never stored."""
    return f"ccsk_{secrets.token_urlsafe(36)}"


def hash_secret(raw):
    return make_password(raw)


def verify_secret(client, raw):
    """True when ``raw`` matches the current secret, or the previous one while
    its rotation grace window is still open."""
    if not raw:
        return False
    if check_password(raw, client.secret_hash):
        return True
    if client.previous_secret_hash and client.previous_secret_expires_at:
        if client.previous_secret_expires_at > timezone.now():
            return check_password(raw, client.previous_secret_hash)
    return False


def hash_token(raw):
    """Access tokens are looked up by hash, so a DB leak yields no usable token."""
    return hashlib.sha256((raw or "").encode()).hexdigest()


def issue_token(client, *, ip=""):
    """Mint an opaque access token for ``client``. Returns ``(raw, instance)``."""
    raw = f"ccat_{secrets.token_urlsafe(40)}"
    token = PartnerAccessToken.objects.create(
        client=client,
        token_hash=hash_token(raw),
        scopes=list(client.scopes or []),
        expires_at=timezone.now() + timezone.timedelta(seconds=TOKEN_TTL_SECONDS),
        created_ip=(ip or "")[:64],
    )
    return raw, token


def client_ip(request):
    fwd = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return fwd or (request.META.get("REMOTE_ADDR") or "")


def ip_allowed(client, ip):
    """Optional per-credential IP allowlist. Empty list = allow any."""
    allowed = list(client.allowed_ips or [])
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if "/" in str(entry):
                if addr in ipaddress.ip_network(str(entry), strict=False):
                    return True
            elif addr == ipaddress.ip_address(str(entry)):
                return True
        except ValueError:
            continue
    return False


def authenticate_client(client_id, client_secret, *, ip=""):
    """Resolve a client_id/secret pair to an active credential, or raise."""
    client = DeliveryCompanyApiClient.objects.filter(
        client_id=(client_id or "").strip()
    ).select_related("delivery_company").first()
    # Same error for unknown vs wrong secret: never confirm a client_id exists.
    if client is None or not verify_secret(client, client_secret):
        raise exceptions.AuthenticationFailed("Invalid client credentials.")
    if not client.is_active:
        raise exceptions.AuthenticationFailed("This credential is revoked or expired.")
    if not ip_allowed(client, ip):
        raise exceptions.AuthenticationFailed("This credential is not allowed from your IP.")
    return client


# --- request principal -----------------------------------------------------
class PartnerPrincipal:
    """The authenticated delivery partner. Not a Django user: it exists only to
    satisfy DRF and to carry the company every query must be scoped to."""

    def __init__(self, client, token):
        self.client = client
        self.token = token
        self.delivery_company = client.delivery_company
        self.delivery_company_id = client.delivery_company_id
        self.scopes = list(token.scopes or [])
        # DRF's throttling keys off request.user.pk -- identifying the principal
        # by its CREDENTIAL means the rate limit is per delivery company.
        self.pk = client.pk
        self.id = client.pk

    # DRF/Django expectations -----------------------------------------------
    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    is_staff = False
    is_superuser = False
    is_anonymous = False

    def has_scope(self, scope):
        return scope in self.scopes

    def __str__(self):
        return f"{self.delivery_company} ({self.client.client_id})"


class DeliveryPartnerAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer <opaque access token>``."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None  # no credentials -> let permissions return 401
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Malformed Authorization header.")
        raw = header[1].decode()

        token = PartnerAccessToken.objects.filter(
            token_hash=hash_token(raw)
        ).select_related("client", "client__delivery_company").first()
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid access token.")
        if not token.is_valid:
            raise exceptions.AuthenticationFailed("Access token expired or revoked.")
        if not token.client.is_active:
            raise exceptions.AuthenticationFailed("This credential is revoked or expired.")

        now = timezone.now()
        ip = client_ip(request)
        PartnerAccessToken.objects.filter(pk=token.pk).update(last_used_at=now)
        DeliveryCompanyApiClient.objects.filter(pk=token.client_id).update(
            last_used_at=now, last_used_ip=(ip or "")[:64]
        )

        principal = PartnerPrincipal(token.client, token)
        return (principal, principal)

    def authenticate_header(self, request):
        return self.keyword


class IsDeliveryPartner(permissions.BasePermission):
    """Authenticated as a delivery partner (belt-and-braces: the view already
    only accepts partner authentication)."""

    message = "Delivery partner credentials are required."

    def has_permission(self, request, view):
        return isinstance(getattr(request, "user", None), PartnerPrincipal)


class HasPartnerScope(permissions.BasePermission):
    """Requires ``view.required_scope`` on the caller's credential."""

    message = "Your credential does not include the required scope."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not isinstance(user, PartnerPrincipal):
            return False
        scope = getattr(view, "required_scope", None)
        return scope is None or user.has_scope(scope)

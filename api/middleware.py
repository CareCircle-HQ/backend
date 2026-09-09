"""Request-scoped middleware."""

from django.conf import settings
from django.db import connection


class PartnerHostMiddleware:
    """Serve ONLY the delivery-partner API on ``settings.PARTNER_API_HOST``.

    Swapping ``request.urlconf`` means the CRM's routes are not merely forbidden
    on that hostname -- they do not exist, so ``/api/clients/`` is a 404. That is
    what lets us hand a vendor's developers a URL without exposing any other part
    of the CRM or the extension API. The reverse also holds: the partner routes
    live in a module ``backend/urls.py`` never includes, so they are absent from
    the main site.

    Must run BEFORE URL resolution, hence its position near the top of
    MIDDLEWARE. Inert (and dropped) when ``PARTNER_API_HOST`` is unset.
    """

    URLCONF = "api.partner.urls"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Read the setting per request (not cached in __init__) so tests can
        # override_settings it, and so it is never stale. Just a string compare.
        expected = (getattr(settings, "PARTNER_API_HOST", "") or "").strip().lower()
        if expected:
            # Host header only (never a forwarded header), so a client cannot
            # select the partner surface -- or escape it -- by spoofing a proxy
            # header.
            host = (request.get_host() or "").split(":")[0].lower()
            if host == expected:
                request.urlconf = self.URLCONF
                request.is_partner_api = True
        return self.get_response(request)


class StatementTimeoutMiddleware:
    """Cap the Postgres statement time for WEB requests only.

    A single pathological query (e.g. the Members list on a bad plan after a big
    purge) could otherwise hold a gunicorn worker + DB connection for minutes,
    and enough of them saturate every worker -> the whole site 504s. Capping each
    web request's statement time makes such a query fail fast instead of taking
    the site down.

    Crucially this runs ONLY for web requests -- management commands (imports,
    purges, backfills, migrations, index builds) don't go through middleware, so
    they can still run long. Configurable via ``WEB_STATEMENT_TIMEOUT_MS`` (ms);
    set to 0 to disable. Applied per request; harmless to re-set on a pooled
    (CONN_MAX_AGE) connection.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.timeout_ms = int(getattr(settings, "WEB_STATEMENT_TIMEOUT_MS", 0) or 0)
        if self.timeout_ms <= 0:
            # Returning without wrapping tells Django to drop this middleware.
            from django.core.exceptions import MiddlewareNotUsed

            raise MiddlewareNotUsed()

    def __call__(self, request):
        try:
            with connection.cursor() as cur:
                # timeout_ms is a validated int -> safe to inline (SET rejects
                # bound parameters for its value).
                cur.execute(f"SET statement_timeout = {self.timeout_ms}")
        except Exception:  # pragma: no cover - never block a request on this
            pass
        return self.get_response(request)

"""Request-scoped middleware."""

import logging
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)


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


class SlowRequestMiddleware:
    """Log any request slower than ``SLOW_REQUEST_MS`` with WHO and WHAT.

    Written after the 2026-09-14 incident, where a single endpoint held gunicorn
    workers for up to 15 minutes and the logs never said so: the duration had to
    be reconstructed afterwards from ImportRun rows, and the endpoint found by
    reading source code. nginx timing (``$request_time``) covers the "what and how
    long" half, but it cannot see WHICH AGENT made the call -- and "an agent says
    it's slow" is how these arrive.

    Deliberately logged at WARNING with a fixed ``SLOW REQUEST`` prefix so a
    CloudWatch metric filter can count it (see docs/observability-plan.md).

    The QUERY STRING is never logged: members-list search puts member names in it,
    and there is no operational reason to accumulate those. The path alone
    identifies the endpoint.

    Placed LAST in MIDDLEWARE so it measures the whole stack beneath it. Inert
    (and dropped) when SLOW_REQUEST_MS is 0.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.threshold_ms = int(getattr(settings, "SLOW_REQUEST_MS", 0) or 0)
        if self.threshold_ms <= 0:
            from django.core.exceptions import MiddlewareNotUsed

            raise MiddlewareNotUsed()

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        elapsed_ms = (time.monotonic() - started) * 1000
        if elapsed_ms >= self.threshold_ms:
            logger.warning(
                "SLOW REQUEST %s %s -> %s in %.0fms (agent=%s)",
                request.method,
                # request.path, never get_full_path(): no query string.
                request.path,
                getattr(response, "status_code", "?"),
                elapsed_ms,
                self._actor(request),
            )
        return response

    @staticmethod
    def _actor(request):
        """The acting agent, best-effort. Never raises and never touches the DB:
        this runs on every slow request, including ones that are slow BECAUSE the
        database is struggling."""
        user = getattr(request, "user", None)
        code = getattr(user, "agent_code", "") or ""
        if code:
            return code
        agent_id = getattr(user, "agent_id", "") or ""
        return str(agent_id) if agent_id else "anonymous"

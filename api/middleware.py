"""Request-scoped middleware."""

from django.conf import settings
from django.db import connection


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

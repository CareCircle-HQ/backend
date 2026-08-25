"""Database routers.

AnalyticsRouter (Phase 2): route READ-ONLY analytics queries -- currently the
Data page's EnrollmentAnalytics read model -- to the 'replica' database when one
is configured (REPLICA_DB_HOST set), keeping heavy analytics reads off the
primary. Writes always go to the primary, and the read-model rebuild job pins
itself to 'default' so it never computes from a lagging replica.

Resilience: reads only route to the replica while it's reachable. A cached
liveness probe (every ~30s) falls back to the PRIMARY if the replica is down, so
a replica hiccup degrades performance instead of erroring the Data page. No
replica configured (local/CI) -> every method returns None and Django uses
'default'. See docs/analytics-architecture.md (Phase 2).
"""

import logging
import os
import time

from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

_REPLICA = "replica"
# Models whose READS may be served from the replica (all lowercase model names).
_ANALYTICS_READ_MODELS = {"enrollmentanalytics"}
# Cache the replica health check so we probe at most once per interval, not on
# every query. Tunable; 0 disables the probe (always trust the replica).
_HEALTH_TTL = int(os.getenv("REPLICA_HEALTHCHECK_SECONDS", "30"))
_health = {"ok": False, "checked": 0.0}


def _replica_configured():
    return _REPLICA in settings.DATABASES


def _replica_healthy():
    """True when the replica is configured AND reachable (cached ~_HEALTH_TTL).
    Falls back to False (=> primary) on any connection error."""
    if not _replica_configured():
        return False
    if _HEALTH_TTL <= 0:
        return True
    now = time.monotonic()
    if now - _health["checked"] < _HEALTH_TTL:
        return _health["ok"]
    ok = True
    try:
        with connections[_REPLICA].cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - replica down/unreachable -> use primary
        ok = False
        try:
            connections[_REPLICA].close()  # drop the broken conn; reconnect next probe
        except Exception:  # noqa: BLE001
            pass
        logger.warning("AnalyticsRouter: replica unreachable, routing reads to primary")
    _health["ok"] = ok
    _health["checked"] = now
    return ok


class AnalyticsRouter:
    def db_for_read(self, model, **hints):
        if model._meta.model_name in _ANALYTICS_READ_MODELS and _replica_healthy():
            return _REPLICA
        return None  # -> default (primary)

    def db_for_write(self, model, **hints):
        return None  # always the primary ('default')

    def allow_relation(self, obj1, obj2, **hints):
        # Same physical data on primary + replica; relations are always fine.
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Never migrate the replica (it's a physical, read-only copy of primary).
        if db == _REPLICA:
            return False
        return None

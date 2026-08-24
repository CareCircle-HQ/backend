"""Database routers.

AnalyticsRouter (Phase 2): route READ-ONLY analytics queries -- currently the
Data page's EnrollmentAnalytics read model -- to the 'replica' database when one
is configured (REPLICA_DB_HOST set), keeping heavy analytics reads off the
primary. Writes always go to the primary, and the read-model rebuild job pins
itself to 'default' so it never computes from a lagging replica. No replica
configured (local/CI) -> every method returns None and Django uses 'default'.

See docs/analytics-architecture.md (Phase 2).
"""

from django.conf import settings

# Models whose READS may be served from the replica (all lowercase model names).
_ANALYTICS_READ_MODELS = {"enrollmentanalytics"}


class AnalyticsRouter:
    @staticmethod
    def _replica():
        return "replica" if "replica" in settings.DATABASES else None

    def db_for_read(self, model, **hints):
        if model._meta.model_name in _ANALYTICS_READ_MODELS:
            return self._replica()  # None -> default
        return None

    def db_for_write(self, model, **hints):
        return None  # always the primary ('default')

    def allow_relation(self, obj1, obj2, **hints):
        # Same physical data on primary + replica; relations are always fine.
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Never migrate the replica (it's a physical, read-only copy of primary).
        if db == "replica":
            return False
        return None

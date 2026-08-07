"""Headless assessment-results enrichment (nightly-assessment-eligibility plan,
Phases 3 + 5).

Pulls each tracked client's eligibility assessments from the Unite Us
screenings-ingestion host and upserts their ``eligible_services`` /
``eligible_status`` through ``AssessmentSerializer`` -- which already drives
``catalog.upsert_programs`` and ``derive_client_level`` (Client Level 1/2). This
is the single source of truth for both the nightly Celery task and the
``import_assessment_results`` management command (backfill + ad-hoc).

``eligible_services`` is immutable once set, so the default target set is only
assessments still MISSING results -- a small trickle (the day's new CSV imports),
not the whole population.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from api.history import ChangeSource, change_context
from api.integrations.uniteus import mappers
from api.integrations.uniteus.api import UniteUsApiError, UniteUsAuthExpired
from api.integrations.uniteus.screenings_api import ScreeningsIngestionClient
from api.models import (
    Assessment,
    ImportRun,
    ImportRunStatus,
)
from api.serializers import AssessmentSerializer
from api.services import timeline
from api.services.uniteus_import import _select_active_credential

logger = logging.getLogger(__name__)

# Cap the "what changed" list carried on the ImportRun stats (mirrors DailyPull).
_PREVIEW_CAP = 200


def target_person_ids(*, client_ids=None, limit=0, since=None):
    """Person ids to enrich. Explicit ``client_ids`` win; otherwise every linked
    assessment still MISSING ``eligible_services`` (optionally created on/after
    ``since``)."""
    if client_ids:
        return [str(c) for c in client_ids]
    qs = (
        Assessment.objects.filter(client__isnull=False)
        .filter(Q(eligible_services=[]) | Q(eligible_services__isnull=True))
    )
    if since is not None:
        qs = qs.filter(screen_created_at__gte=since)
    ids = [str(x) for x in qs.values_list("subject_id", flat=True).distinct()]
    if limit:
        ids = ids[:limit]
    return ids


class AssessmentEnricher:
    """Runs the list -> detail -> map -> upsert loop, isolating per-record
    failures and rolling counts into an ``ImportRun``-friendly stats dict."""

    def __init__(self, *, provider_id=None, apply=True, allow_refresh=False):
        self.provider_id = provider_id
        self.apply = apply
        self.allow_refresh = allow_refresh
        self.api = None
        self.cred = None
        self.errors = []
        self.stats = {"clients": 0, "assessments": 0, "enriched": 0, "errors": 0}
        self.previews = []

    def _bind_credential(self):
        cred = _select_active_credential(provider_id=self.provider_id)
        if cred is None:
            self.errors.append("No active Unite Us credential; nothing to enrich.")
            return False
        try:
            _ = cred.refresh_token  # force decrypt; a rotated-key cred is unusable
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"credential {cred.pk} unreadable: {exc}")
            return False
        self.cred = cred
        self.provider_id = self.provider_id or cred.provider_id
        self.api = ScreeningsIngestionClient(cred, allow_refresh=self.allow_refresh)
        return True

    def run(self, person_ids):
        if not self._bind_credential():
            return self.stats
        for pid in person_ids:
            self.stats["clients"] += 1
            try:
                self._enrich_client(pid)
            except UniteUsAuthExpired as exc:
                # Session expired mid-run: stop cleanly, the caller surfaces a
                # reconnect message (an agent must re-login via the extension).
                self.errors.append(f"credential {self.cred.pk} expired: {exc}")
                break
            except UniteUsApiError as exc:
                self.stats["errors"] += 1
                self.errors.append(f"client {pid}: {exc}")
        return self.stats

    def _enrich_client(self, person_id):
        for summary in self.api.list_assessments(person_id, provider_id=self.provider_id):
            self.stats["assessments"] += 1
            sid = summary.get("id")
            if not sid:
                continue
            try:
                detail = self.api.get_screen_detail(sid)
            except UniteUsApiError:
                detail = {}
            data = mappers.map_assessment_api(detail, summary, person_id=person_id)
            services = data.get("eligible_services") or []
            if not services:
                continue
            self.stats["enriched"] += 1
            if len(self.previews) < _PREVIEW_CAP:
                self.previews.append(
                    {"person_id": person_id, "assessment_id": str(sid),
                     "eligible_services": services}
                )
            if not self.apply:
                continue
            ser = AssessmentSerializer(data=data)
            if ser.is_valid():
                assessment = ser.save()
                # Back-fill the timeline event's eligibility/results metadata now
                # that eligible_services has arrived (the CSV import created a
                # results-less row). resync=True updates just those metadata keys
                # on the existing event, or creates it if none exists yet.
                timeline.event_for_assessment(
                    assessment,
                    source=ChangeSource.IMPORT,
                    actor="system:assessment-results",
                    resync=True,
                )
            else:
                self.stats["errors"] += 1
                self.errors.append(f"assessment {sid} invalid: {ser.errors}")


def enrich_assessments(*, client_ids=None, provider_id=None, apply=True,
                       allow_refresh=False, limit=0, since=None):
    """Enrich assessments in-process (no ImportRun). Returns the enricher so the
    caller can read ``stats`` / ``previews`` / ``errors``. Used by the command's
    dry-run + the ImportRun-wrapped nightly task below."""
    person_ids = target_person_ids(client_ids=client_ids, limit=limit, since=since)
    enricher = AssessmentEnricher(
        provider_id=provider_id, apply=apply, allow_refresh=allow_refresh
    )
    if person_ids:
        enricher.run(person_ids)
    return enricher


def run_assessment_enrichment(*, triggered_by="cron:assessment-results",
                              client_ids=None, provider_id=None, limit=0,
                              since=None, allow_refresh=True):
    """Execute one enrichment pass wrapped in an ``ImportRun`` + import change
    context (so history rows are tagged source='import'). Always writes
    (``apply=True``). Returns the persisted ``ImportRun``."""
    run = ImportRun.objects.create(
        source="uniteus_assessments",
        status=ImportRunStatus.RUNNING,
        triggered_by=triggered_by,
    )
    enricher = AssessmentEnricher(
        provider_id=provider_id, apply=True, allow_refresh=allow_refresh
    )
    try:
        person_ids = target_person_ids(
            client_ids=client_ids, limit=limit, since=since
        )
        with change_context(ChangeSource.IMPORT, "system:assessment-results"):
            enricher.run(person_ids)
        run.status = ImportRunStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001
        run.status = ImportRunStatus.FAILED
        enricher.errors.append(f"FATAL: {exc}")
        logger.exception("assessment enrichment aborted")
    finally:
        s = enricher.stats
        run.stats = dict(s)
        if enricher.previews:
            run.stats["enriched_preview"] = enricher.previews
        run.updated_count = s["enriched"]
        run.error_count = s["errors"]
        run.processed_count = s["assessments"]
        run.finished_at = timezone.now()
        if enricher.errors:
            run.error_log = "\n".join(enricher.errors)[:10000]
        run.save()
    return run

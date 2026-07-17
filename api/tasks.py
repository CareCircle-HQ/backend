"""Celery tasks for the CareCircle backend.

Currently: async processing of manual CSV imports uploaded to S3. The web
request only creates the ImportRun + presigns the upload; the heavy import runs
here in the worker so it survives request timeouts and the agent closing the
tab. Progress + final status are written to the ImportRun row (polled by the
Settings > Import UI).
"""
import logging
import os

from celery import shared_task
from django.utils import timezone

from .models import ImportRun, ImportRunStatus
from .services import import_storage
from .services.csv_import import run_csv_import

logger = logging.getLogger(__name__)


@shared_task(bind=True, ignore_result=True)
def process_import(self, run_id):
    """Download the uploaded CSV from S3 and run the import for ``run_id``."""
    run = ImportRun.objects.filter(pk=run_id).first()
    if run is None:
        logger.warning("process_import: ImportRun %s not found", run_id)
        return
    if not run.file_key:
        run.status = ImportRunStatus.FAILED
        run.error_log = "No file_key on the import run -- nothing to process."
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error_log", "finished_at"])
        return

    tmp = None
    try:
        tmp = import_storage.download_to_temp(run.file_key)
        # run_csv_import owns status transitions (RUNNING -> COMPLETED/FAILED),
        # progress flushing, and the final save() for this run.
        run_csv_import(
            export_type=run.export_type,
            file_obj=tmp,
            triggered_by=run.triggered_by or "manual",
            run=run,
            # Imports keep the derived state fresh (enrollment reconcile, funnel,
            # Care Management warnings) but do NOT open follow-up tickets or write
            # audit timeline events -- Care Management is the source of truth for
            # what needs attention, and those writes were the bulk of the DB load.
            create_tickets=False,
            emit_timeline=False,
        )
    except Exception as exc:  # download / decode / unexpected failure
        logger.exception("process_import %s failed", run_id)
        run.refresh_from_db()
        run.status = ImportRunStatus.FAILED
        run.error_log = ((run.error_log or "") + f"\nFATAL: {exc}").strip()[:10000]
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error_log", "finished_at"])
    finally:
        if tmp is not None:
            try:
                tmp.close()
                os.unlink(tmp.name)
            except OSError:
                pass


@shared_task(bind=True, ignore_result=True)
def poll_uniteus_exports(self, limit=50):
    """Advance every pending Unite Us export (poll state -> download -> import ->
    reconcile). Scheduled on Celery beat; also safe to call ad-hoc."""
    from .services import uniteus_exports
    processed = uniteus_exports.poll_pending(limit=limit)
    logger.info("poll_uniteus_exports: processed %s export(s)", len(processed))


@shared_task(bind=True, ignore_result=True)
def sync_member_warnings(self, limit=None):
    """Refresh the member/household warning snapshot across every servable
    household. Safety net for TIME-BASED checks (e.g. an insurance or
    internal-service authorization that lapses with the passing of a day) that
    no write would otherwise re-trigger. Scheduled daily on Celery beat; also
    safe to call ad-hoc. Delegates to the management command so the sweep logic
    lives in one place."""
    from django.core.management import call_command

    call_command("sync_member_warnings", *(["--limit", str(limit)] if limit else []))


@shared_task(bind=True, ignore_result=True)
def sync_delivery_calendars(self, from_date=None):
    """Reconcile the delivery calendar for every active household so no eligible
    member is missing from upcoming Purchase Orders: a member ADDED to an
    already-active household gets a plan + occurrences, and PAUSED/removed
    members have their future occurrences dropped (dates already batched into a
    PO are never touched). Scheduled daily on Celery beat; also safe to call
    ad-hoc. Delegates to the management command so the logic lives in one
    place."""
    from django.core.management import call_command

    call_command(
        "sync_delivery_calendars", *(["--from", str(from_date)] if from_date else [])
    )


@shared_task(bind=True, ignore_result=True)
def request_uniteus_exports(self, export_types=None, days=7, triggered_by="cron:uniteus-export"):
    """Request a rolling-window export for each of ``export_types`` (default: all
    supported), then kick a poll. Used by the nightly schedule; the UI requests
    inline instead."""
    from datetime import timedelta
    from django.utils import timezone
    from .services import uniteus_exports

    types = export_types or list(uniteus_exports.SUPPORTED_EXPORT_TYPES)
    end = timezone.localdate()
    start = end - timedelta(days=max(days, uniteus_exports.MIN_WINDOW_DAYS))
    for etype in types:
        try:
            uniteus_exports.request_export(
                etype, start, end, triggered_by=triggered_by,
            )
        except Exception:  # noqa: BLE001 - one type failing shouldn't stop the rest
            logger.exception("request_uniteus_exports: %s failed", etype)
    # Nudge the poller so freshly-requested exports start advancing.
    poll_uniteus_exports.delay()

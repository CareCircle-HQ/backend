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

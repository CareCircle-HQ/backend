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


# ImportRun.source for the manual "Prepare Members for PO" job so it is tracked
# with the same progress fields the UI already polls, without polluting the CSV
# import history (which filters on CSV_SOURCE).
MEMBER_PREP_SOURCE = "member_prep"


@shared_task(bind=True, ignore_result=True)
def prepare_members_for_po(self, run_id):
    """Reconcile the delivery calendar for EVERY active household in one
    background pass, writing live progress to the tracking ``ImportRun`` so the
    Orders page can show a progress bar (mirrors the S3 CSV import flow).

    This is the manual, agent-triggered "Prepare Members for PO" action: it adds
    newly-eligible members to the calendar, drops occurrences for members who are
    no longer part of a servable household (closed/unauthorized/paused), and
    frees dates whose PO was cancelled -- so the next Purchase Order preview is
    accurate. Runs in the worker because a full-calendar reconcile is far too
    slow for a web request (it 504s inline).
    """
    run = ImportRun.objects.filter(pk=run_id).first()
    if run is None:
        logger.warning("prepare_members_for_po: ImportRun %s not found", run_id)
        return

    from .services.orders import sync_active_calendars

    run.status = ImportRunStatus.RUNNING
    run.save(update_fields=["status"])

    # Throttle progress writes: the callback fires per enrollment (thousands of
    # them), so only flush the tracking row every N to keep it cheap while still
    # giving the UI a live percentage.
    state = {"last": 0}

    def _progress(processed, total):
        # Keep the in-memory instance in sync too, so the final save() in the
        # finally block doesn't clobber the flushed counts with stale values.
        if run.progress_total != total:
            ImportRun.objects.filter(pk=run.pk).update(
                progress_total=total, processed_count=processed,
            )
            run.progress_total = total
            run.processed_count = processed
            state["last"] = processed
            return
        if processed - state["last"] >= 50 or processed == total:
            ImportRun.objects.filter(pk=run.pk).update(processed_count=processed)
            run.processed_count = processed
            state["last"] = processed

    try:
        totals = sync_active_calendars(progress_cb=_progress)
        run.stats = {"member_prep": totals}
        run.status = ImportRunStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
        logger.exception("prepare_members_for_po %s failed", run_id)
        run.status = ImportRunStatus.FAILED
        run.error_log = f"FATAL: {exc}"[:10000]
    finally:
        run.finished_at = timezone.now()
        run.save(update_fields=[
            "status", "stats", "error_log", "finished_at", "processed_count",
            "progress_total",
        ])


@shared_task(bind=True, ignore_result=True)
def sweep_closed_case_service(self):
    """Safety-net sweep: cancel service for any client whose LAST internal-service
    (meal/box) case has closed but whose enrollment was never terminalized (the
    close-out never ran -- e.g. a historical closure on a client not since
    re-synced). Re-runs the idempotent internal-service reconcile so their future
    deliveries are truncated and the enrollment(s) cancelled -- dropping them off
    Purchase Orders and the delivery calendar. A no-op once everything is clean.
    Scheduled daily on Celery beat; also safe to call ad-hoc. Delegates to the
    management command so the sweep logic lives in one place."""
    from django.core.management import call_command

    call_command("stop_closed_case_service", "--all", "--apply")


@shared_task(bind=True, ignore_result=True)
def process_reauthorization_extensions(self):
    """Daily safety-net sweep: activate / gap-pause parked reauthorization
    (service-extension) enrollments by the calendar. A parked SCHEDULED_EXTENSION
    is promoted to Service Active once its authorization window begins (closing
    the current enrollment), or its members are paused during a gap between the
    current window's end and the reauth window's start. A no-op once everything is
    aligned. Delegates to the management command so the logic lives in one place."""
    from django.core.management import call_command

    call_command("process_reauthorization_extensions", "--apply")


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


@shared_task(bind=True, ignore_result=True)
def import_uniteus_assessment_results(self, limit=0, since=None):
    """Nightly: enrich assessments missing ``eligible_services`` from the Unite
    Us screenings-ingestion host (drives catalog + client Level 1/2). Gated by
    ``UNITEUS_ASSESSMENT_API_ENABLED`` so it can be dark-launched. Scheduled to
    run AFTER the CSV assessments export lands (which is what creates the
    results-less rows this fills in)."""
    from django.conf import settings

    if not getattr(settings, "UNITEUS_ASSESSMENT_API_ENABLED", False):
        logger.info("assessment-results enrichment skipped (flag off)")
        return
    from .services.assessment_enrichment import run_assessment_enrichment

    run = run_assessment_enrichment(limit=limit, since=since)
    logger.info(
        "assessment-results enrichment: run=%s status=%s enriched=%s errors=%s",
        run.pk, run.status, run.updated_count, run.error_count,
    )


@shared_task(bind=True, ignore_result=True)
def generate_report_export(self, export_id):
    """Build an Admin > Reports CSV in the background, upload it to S3, and flip
    the ReportExport status the UI polls. Mirrors process_import: the heavy work
    runs here so it survives request timeouts. Best-effort status on failure."""
    import csv
    import tempfile

    from .models import ReportExport, ReportExportStatus
    from .portal.report_exports import REPORT_BUILDERS, default_filename

    export = ReportExport.objects.filter(pk=export_id).first()
    if export is None:
        logger.warning("generate_report_export: ReportExport %s not found", export_id)
        return

    builder = REPORT_BUILDERS.get(export.report_key)
    if builder is None:
        export.status = ReportExportStatus.FAILED
        export.error_log = f"Unknown report_key: {export.report_key!r}"
        export.finished_at = timezone.now()
        export.save(update_fields=["status", "error_log", "finished_at"])
        return

    export.status = ReportExportStatus.RUNNING
    export.save(update_fields=["status"])

    tmp = None
    try:
        filename = export.filename or default_filename(export.report_key)
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", suffix=".csv", newline="", delete=False,
        )
        writer = csv.writer(tmp)
        data_rows = 0
        for i, row in enumerate(builder(export.params or {})):
            writer.writerow(row)
            if i > 0:  # first row is the header
                data_rows += 1
        tmp.flush()
        tmp.close()

        key = import_storage.build_export_key(filename)
        with open(tmp.name, "rb") as fh:
            import_storage.upload_fileobj(key, fh)

        export.status = ReportExportStatus.COMPLETED
        export.file_key = key
        export.filename = filename
        export.row_count = data_rows
        export.finished_at = timezone.now()
        export.save(update_fields=[
            "status", "file_key", "filename", "row_count", "finished_at",
        ])
    except Exception as exc:  # noqa: BLE001 - always record the failure
        logger.exception("generate_report_export failed for %s", export_id)
        export.status = ReportExportStatus.FAILED
        export.error_log = str(exc)[:2000]
        export.finished_at = timezone.now()
        export.save(update_fields=["status", "error_log", "finished_at"])
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

"""Automate Unite Us "Exports": request -> poll -> download -> S3 -> import.

The Exports page (app.uniteus.io/exports) is backed by the core API we already
integrate with. This service:

1. ``request_export`` -- POSTs a new export (type + date window) and records a
   :class:`UniteUsExport` row (status=requested).
2. ``poll_pending`` -- advances requested/importing rows: checks Unite Us'
   ``state``; when ``completed`` it resolves the file's (short-lived) download
   URL, streams the CSV to S3, and hands it to the SAME import pipeline the
   manual Settings > Import upload uses (ImportRun + Celery ``process_import``),
   so all rules + activity tracking apply. Reconciles status once the ImportRun
   finishes.

Idempotent: keyed on the Unite Us export id, so an export is never imported
twice. Auth reuses an ACTIVE captured credential (like the daily pull).
"""
import logging
import os
import tempfile

from django.utils import timezone

from api.integrations.uniteus import api as uu_api
from api.integrations.uniteus.api import UniteUsApiError, UniteUsAuthExpired
from api.models import (
    ImportRun,
    ImportRunStatus,
    UniteUsCredential,
    UniteUsCredentialStatus,
    UniteUsExport,
    UniteUsExportStatus,
)
from api.services import import_storage
from api.services.csv_import import CSV_SOURCE, run_csv_import

logger = logging.getLogger(__name__)

# Unite Us export_type -> our CSV importer type. These are the only types we
# support automating (the Request Export UI offers exactly these).
EXPORT_TYPE_TO_IMPORTER = {
    "clients": "clients",
    "assessments": "assessments",
    "cases": "cases",
    "notes": "notes",
    "screeningsv2": "screening",  # importer's "screening" reads the v2 export
}
SUPPORTED_EXPORT_TYPES = tuple(EXPORT_TYPE_TO_IMPORTER)

# The Request Export UI enforces a minimum reporting window of 7 days.
MIN_WINDOW_DAYS = 7

# Unite Us states that mean the export can't produce a file.
_FAILED_STATES = {"failed", "errored", "error", "cancelled", "canceled"}
_DONE_STATE = "completed"


def _active_credential(provider_id=None):
    """Pick the credential to run automation with.

    Prefers a credential explicitly flagged ``for_automation`` (a dedicated
    Unite Us service account) so a server-side token refresh never rotates -- and
    logs out -- a real agent's live browser session. Falls back to the newest
    ACTIVE captured credential. Defers the encrypted token columns so selection
    never eagerly decrypts."""
    base = (
        UniteUsCredential.objects.filter(status=UniteUsCredentialStatus.ACTIVE)
        .defer("access_token", "refresh_token")
    )
    if provider_id:
        base = base.filter(provider_id=provider_id)
    # 1) Dedicated automation credential (most recently captured, if several).
    dedicated = base.filter(for_automation=True).order_by(
        "-last_captured_at", "-updated_at"
    ).first()
    if dedicated is not None:
        return dedicated
    # 2) Fallback: newest ACTIVE credential (usually an actively-working agent,
    #    so within the recently_captured window -> used as-is, no rotation).
    return base.order_by("-last_captured_at", "-updated_at").first()


def request_export(export_type, start_date, end_date, *, triggered_by="manual",
                   provider_id=None):
    """Request a new Unite Us export and persist a UniteUsExport row.

    ``start_date`` / ``end_date`` are ``datetime.date``. Raises ValueError for an
    unsupported type or a window shorter than MIN_WINDOW_DAYS, and RuntimeError
    when no usable credential exists."""
    if export_type not in EXPORT_TYPE_TO_IMPORTER:
        raise ValueError(
            f"Unsupported export type '{export_type}'. "
            f"Supported: {', '.join(SUPPORTED_EXPORT_TYPES)}."
        )
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date.")
    if (end_date - start_date).days < MIN_WINDOW_DAYS:
        raise ValueError(f"The reporting window must be at least {MIN_WINDOW_DAYS} days.")

    cred = _active_credential(provider_id)
    if cred is None:
        raise RuntimeError(
            "No active Unite Us credential. An agent must be logged into Unite "
            "Us (so the extension captures a session) before exports can be requested."
        )

    client = uu_api.UniteUsClient(cred)
    rec = client.request_export(
        export_type, start_date.isoformat(), end_date.isoformat(),
    )
    attrs = rec.get("attributes") or {}
    exp = UniteUsExport.objects.create(
        export_id=rec.get("id") or "",
        export_type=export_type,
        importer_type=EXPORT_TYPE_TO_IMPORTER[export_type],
        start_date=start_date,
        end_date=end_date,
        unite_state=attrs.get("state", "") or "",
        status=UniteUsExportStatus.REQUESTED,
        provider_id=cred.provider_id,
        triggered_by=triggered_by,
    )
    logger.info("Requested Unite Us export %s (%s) -> row #%s",
                exp.export_id, export_type, exp.pk)
    return exp


def poll_pending(*, limit=50):
    """Advance every not-yet-imported export. Returns the list processed."""
    pending = list(
        UniteUsExport.objects.filter(
            status__in=[UniteUsExportStatus.REQUESTED, UniteUsExportStatus.IMPORTING]
        ).order_by("created_at")[:limit]
    )
    for exp in pending:
        try:
            _advance(exp)
        except (UniteUsApiError, UniteUsAuthExpired) as exc:
            logger.warning("poll export %s failed: %s", exp.export_id, exc)
        except Exception:  # noqa: BLE001 - never let one bad export kill the batch
            logger.exception("poll export %s crashed", exp.export_id)
    return pending


def _advance(exp):
    """Move a single export forward one step based on its current status."""
    # Already handed to the importer: reconcile from the ImportRun's outcome.
    if exp.status == UniteUsExportStatus.IMPORTING:
        _reconcile_import(exp)
        return

    cred = _active_credential(exp.provider_id) or _active_credential()
    if cred is None:
        logger.warning("no active credential to poll export %s", exp.export_id)
        return
    client = uu_api.UniteUsClient(cred)

    rec = client.get_export(exp.export_id)
    state = (rec.get("attributes") or {}).get("state", "") or ""
    if state and state != exp.unite_state:
        exp.unite_state = state
        exp.save(update_fields=["unite_state", "updated_at"])

    if state in _FAILED_STATES:
        _fail(exp, f"Unite Us reported state '{state}'.")
        return
    if state != _DONE_STATE:
        return  # still generating -- try again next poll

    # Completed: resolve the (short-lived) download URL and ingest immediately.
    fu = client.list_export_file_uploads(exp.export_id)
    data = fu.get("data") or []
    if not data:
        logger.info("export %s completed but no file_uploads yet", exp.export_id)
        return
    attrs = data[0].get("attributes") or {}
    path = attrs.get("path")
    if not path:
        return
    exp.file_upload_id = data[0].get("id") or ""
    exp.filename = attrs.get("filename") or f"{exp.export_type}_export.csv"
    exp.save(update_fields=["file_upload_id", "filename", "updated_at"])

    _download_and_import(client, exp, path)


def _download_and_import(client, exp, path):
    """Stream the CSV to a temp file, then either push to S3 + enqueue the async
    importer (prod) or run the import inline (no S3 configured, e.g. dev)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    try:
        client.download_export_file(path, tmp)
        tmp.flush()
        exp.downloaded_at = timezone.now()

        triggered_by = exp.triggered_by or "cron:uniteus-export"
        if import_storage.s3_enabled():
            key = import_storage.build_key(exp.filename)
            with open(tmp.name, "rb") as fh:
                import_storage.upload_fileobj(key, fh)
            run = ImportRun.objects.create(
                source=CSV_SOURCE,
                status=ImportRunStatus.PENDING,
                triggered_by=triggered_by,
                export_type=exp.importer_type,
                file_key=key,
                original_filename=exp.filename[:255],
            )
            exp.import_run = run
            exp.status = UniteUsExportStatus.IMPORTING
            exp.save(update_fields=[
                "import_run", "status", "downloaded_at", "updated_at",
            ])
            # Defer the heavy import to the worker (survives request timeouts).
            from api.tasks import process_import
            process_import.delay(run.pk)
        else:
            # No S3 (dev): import inline from the temp file.
            tmp.seek(0)
            run = run_csv_import(
                export_type=exp.importer_type, file_obj=tmp,
                triggered_by=triggered_by, create_tickets=False,
                emit_timeline=False,
            )
            exp.import_run = run
            exp.status = (
                UniteUsExportStatus.IMPORTED
                if run.status == ImportRunStatus.COMPLETED
                else UniteUsExportStatus.FAILED
            )
            exp.imported_at = timezone.now()
            if run.status != ImportRunStatus.COMPLETED:
                exp.error_log = (run.error_log or "")[:5000]
            exp.save()
    finally:
        try:
            tmp.close()
            os.unlink(tmp.name)
        except OSError:
            pass


def _reconcile_import(exp):
    """Once the ImportRun the export fed into finishes, mirror its terminal
    state onto the export row."""
    run = exp.import_run
    if run is None:
        _fail(exp, "Importing but no ImportRun attached.")
        return
    run.refresh_from_db()
    if run.status == ImportRunStatus.COMPLETED:
        exp.status = UniteUsExportStatus.IMPORTED
        exp.imported_at = timezone.now()
        exp.save(update_fields=["status", "imported_at", "updated_at"])
    elif run.status == ImportRunStatus.FAILED:
        _fail(exp, (run.error_log or "Import failed.")[:5000])
    # else still PENDING/RUNNING -- check again next poll.


def _fail(exp, message):
    exp.status = UniteUsExportStatus.FAILED
    exp.error_log = (message or "")[:5000]
    exp.save(update_fields=["status", "error_log", "updated_at"])


def delete_export(exp):
    """Delete a requested export and the pipeline artifacts it created.

    Removes the :class:`UniteUsExport` row, its linked ``ImportRun`` (and the
    downloaded CSV in S3), and unlinks any tickets that import opened (their
    ``import_run`` FK is ``SET_NULL``, so they survive as standalone tickets).
    The domain records the import ingested (Clients/Cases/Screenings/...) are
    upserts of shared data and are intentionally LEFT IN PLACE. Deleting the row
    also stops the poller from ever re-processing this export.

    Returns a small dict describing what was cleaned up. Safe/idempotent enough
    to call once per row; wrap the caller in a transaction.
    """
    summary = {
        "export_id": exp.export_id,
        "import_run_deleted": False,
        "s3_file_deleted": False,
        "tickets_unlinked": 0,
    }
    run = exp.import_run
    if run is not None:
        # Drop the request's own reference first so deleting the run doesn't try
        # to cascade back through this row.
        exp.import_run = None
        exp.save(update_fields=["import_run", "updated_at"])

        if run.file_key:
            summary["s3_file_deleted"] = import_storage.delete_object(run.file_key)

        # Detach tickets before the run goes away (FK is SET_NULL, but count them
        # for the caller's report).
        summary["tickets_unlinked"] = run.tickets.count()

        run.delete()
        summary["import_run_deleted"] = True

    exp.delete()
    logger.info("Deleted Unite Us export %s (%s)", summary["export_id"], summary)
    return summary

"""Settings > Import: manual Unite Us CSV upload (initial setup + backup) and
the Unite Us agents allowlist that gates which cases the import accepts."""

import uuid

from django.db.models import Count
from django.utils import timezone
from rest_framework import status as http
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from ..models import Case, ImportRun, ImportRunStatus, UniteUsAgent
from ..services import import_storage, uniteus_exports
from ..services.csv_import import (
    CSV_SOURCE,
    SUPPORTED_EXPORT_TYPES,
    run_csv_import,
)
from ..tasks import poll_uniteus_exports, process_import
from .base import PortalAPIView, current_agent


def _progress_percent(run):
    """Integer 0-100 for the UI bar; None while the denominator is unknown."""
    total = run.progress_total
    if not total:
        return None
    return min(100, round(100 * (run.processed_count or 0) / total))


def _run_summary(run):
    return {
        "id": run.pk,
        "source": run.source,
        # Prefer the explicit export_type; fall back to the stats key (older runs
        # predate the field) so the UI can always label the run correctly.
        "dataset": run.export_type or next(iter((run.stats or {}).keys()), ""),
        "export_type": run.export_type,
        "original_filename": run.original_filename,
        "status": run.status,
        "status_label": run.get_status_display(),
        "triggered_by": run.triggered_by,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stats": run.stats,
        # Case imports: aggregate of follow-up actions detected (preview) or
        # applied, plus a capped list of the individual tickets for review.
        "actions": (run.stats or {}).get("actions"),
        "planned_actions": run.planned_actions or [],
        "processed": run.processed_count,
        "progress_total": run.progress_total,
        "progress_percent": _progress_percent(run),
        "created": run.created_count,
        "updated": run.updated_count,
        "skipped": run.skipped_count,
        "errors": run.error_count,
        "error_log": run.error_log,
    }


def _triggered_by(request):
    agent = current_agent(request)
    return f"agent:{agent.agent_code}" if agent and agent.agent_code else "manual"


# Max upload size. The clients export is a few MB, but the denormalized
# screening export (one row per answer) runs to several hundred MB.
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024


class ImportUploadView(PortalAPIView):
    """POST a Unite Us CSV export to import it.

    multipart/form-data: ``file`` (the CSV) + ``export_type`` (one of the
    supported types, currently ``clients``). Runs synchronously and returns the
    resulting ImportRun summary.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        export_type = (request.data.get("export_type") or "").strip().lower()
        if export_type not in SUPPORTED_EXPORT_TYPES:
            return Response(
                {
                    "export_type": (
                        f"Unsupported export type. Supported: "
                        f"{', '.join(SUPPORTED_EXPORT_TYPES)}."
                    )
                },
                status=http.HTTP_400_BAD_REQUEST,
            )

        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"file": "A CSV file is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if upload.size and upload.size > _MAX_UPLOAD_BYTES:
            return Response(
                {"file": "File is too large (max 512 MB)."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        name = (upload.name or "").lower()
        if not name.endswith(".csv"):
            return Response(
                {"file": "Please upload a .csv file."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        agent = current_agent(request)
        triggered_by = f"agent:{agent.agent_code}" if agent and agent.agent_code else "manual"
        run = run_csv_import(
            export_type=export_type, file_obj=upload, triggered_by=triggered_by,
            # Imports keep the derived state fresh (enrollment reconcile, funnel,
            # Care Management warnings) but do NOT open follow-up tickets or write
            # audit timeline events -- Care Management is the source of truth for
            # what needs attention.
            create_tickets=False,
            emit_timeline=False,
        )
        status_code = (
            http.HTTP_200_OK if run.status == "completed" else http.HTTP_400_BAD_REQUEST
        )
        return Response(_run_summary(run), status=status_code)


class ImportRunsView(PortalAPIView):
    """GET the most recent CSV import runs (for the Settings > Import history)."""

    def get(self, request):
        # Only the current calendar month's imports, so the history list stays
        # focused (older runs are still reachable via Import Activity).
        month_start = timezone.localtime().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        runs = (
            ImportRun.objects.filter(source=CSV_SOURCE, started_at__gte=month_start)
            .order_by("-started_at")
        )
        return Response(
            {
                "supported_export_types": list(SUPPORTED_EXPORT_TYPES),
                # When true the UI uploads direct to S3 (presign -> PUT -> start)
                # and polls; otherwise it falls back to the synchronous upload.
                "async_uploads": import_storage.s3_enabled(),
                "max_upload_bytes": _MAX_UPLOAD_BYTES,
                "results": [_run_summary(r) for r in runs],
            }
        )


class ImportRunDetailView(PortalAPIView):
    """GET a single import run -- polled by the UI for live progress/status."""

    def get(self, request, run_id):
        run = ImportRun.objects.filter(pk=run_id, source=CSV_SOURCE).first()
        if run is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        return Response(_run_summary(run))


class ImportPresignView(PortalAPIView):
    """Step 1 of the async upload: validate the request, create a pending
    ImportRun, and return a short-lived presigned S3 PUT URL the browser uploads
    the file directly to (bypassing gunicorn/nginx timeouts + body limits)."""

    def post(self, request):
        if not import_storage.s3_enabled():
            return Response(
                {"detail": "Direct uploads are not configured (no S3 bucket)."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        export_type = (request.data.get("export_type") or "").strip().lower()
        if export_type not in SUPPORTED_EXPORT_TYPES:
            return Response(
                {"export_type": (
                    f"Unsupported export type. Supported: "
                    f"{', '.join(SUPPORTED_EXPORT_TYPES)}."
                )},
                status=http.HTTP_400_BAD_REQUEST,
            )

        filename = (request.data.get("filename") or "").strip()
        if not filename.lower().endswith(".csv"):
            return Response(
                {"filename": "Please upload a .csv file."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        size = request.data.get("size")
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        if size is not None and size > _MAX_UPLOAD_BYTES:
            return Response(
                {"file": "File is too large (max 512 MB)."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        key = import_storage.build_key(filename)
        run = ImportRun.objects.create(
            source=CSV_SOURCE,
            status=ImportRunStatus.PENDING,
            triggered_by=_triggered_by(request),
            export_type=export_type,
            file_key=key,
            original_filename=filename[:255],
        )
        try:
            upload_url = import_storage.presign_put(key, content_type="text/csv")
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
            run.status = ImportRunStatus.FAILED
            run.error_log = f"Could not presign upload: {exc}"
            run.save(update_fields=["status", "error_log"])
            return Response(
                {"detail": "Could not create an upload URL."},
                status=http.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "run_id": run.pk,
                "upload_url": upload_url,
                "key": key,
                # The browser MUST PUT with this exact Content-Type -- it's part
                # of the signature.
                "content_type": "text/csv",
            },
            status=http.HTTP_201_CREATED,
        )


class ImportStartView(PortalAPIView):
    """Step 2 of the async upload: after the browser finishes the S3 PUT, verify
    the object landed and enqueue the Celery worker to process it."""

    def post(self, request, run_id):
        run = ImportRun.objects.filter(pk=run_id, source=CSV_SOURCE).first()
        if run is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        if run.status != ImportRunStatus.PENDING:
            # Already started/finished -- return current state (idempotent).
            return Response(_run_summary(run))
        if not run.file_key or not import_storage.object_exists(run.file_key):
            return Response(
                {"detail": "Uploaded file was not found in storage."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        process_import.delay(run.pk)
        return Response(_run_summary(run), status=http.HTTP_202_ACCEPTED)


class ImportActivityView(PortalAPIView):
    """Settings > Import Activity: a rollup of the follow-up actions detected
    (and, once applied, created) across case imports -- cases closed,
    authorization changes, and the tickets each would open -- so an agent can
    review the work an import produces in one place before we start creating
    tickets automatically."""

    _RESULT_CAP = 500

    def get(self, request):
        # Every recent CSV import (not just action-bearing ones) so the dropdown
        # can list clients/screenings/etc. too -- they show record counts even
        # though only case imports produce follow-up actions.
        base = ImportRun.objects.filter(source=CSV_SOURCE).order_by("-started_at")
        recent = list(base[:50])
        run_options = [{
            "run_id": r.pk,
            "started_at": r.started_at,
            "dataset": r.export_type or next(iter((r.stats or {}).keys()), "") or "import",
            "original_filename": r.original_filename,
            "triggered_by": r.triggered_by,
            "status": r.status,
            "created": r.created_count,
            "updated": r.updated_count,
            "skipped": r.skipped_count,
            "errors": r.error_count,
            "applied": bool(((r.stats or {}).get("actions") or {}).get("applied")),
            "tickets": int(((r.stats or {}).get("actions") or {}).get("tickets") or 0),
        } for r in recent]

        # Scope: a single run when ?run_id= is given (looked up directly so it
        # works even for runs older than the recent-50 window), else all recent.
        run_id = (request.query_params.get("run_id") or "").strip()
        if run_id:
            runs = list(base.filter(pk=run_id))
        else:
            runs = recent

        totals = {
            "runs": 0, "tickets": 0, "tickets_created": 0, "cases_closed": 0,
            "auth_changed": 0, "timeline_events": 0,
        }
        # Record-level counts (meaningful for every import type, incl. clients).
        record_totals = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        auth_to = {}
        results = []
        any_applied = False
        for run in runs:
            totals["runs"] += 1
            record_totals["created"] += run.created_count
            record_totals["updated"] += run.updated_count
            record_totals["skipped"] += run.skipped_count
            record_totals["errors"] += run.error_count

            actions = (run.stats or {}).get("actions") or {}
            if not actions:
                continue
            for key in ("tickets", "tickets_created", "cases_closed",
                        "auth_changed", "timeline_events"):
                totals[key] += int(actions.get(key) or 0)
            for status, count in (actions.get("auth_changed_to") or {}).items():
                auth_to[status] = auth_to.get(status, 0) + int(count or 0)
            applied = bool(actions.get("applied"))
            any_applied = any_applied or applied
            for pa in (run.planned_actions or []):
                if len(results) >= self._RESULT_CAP:
                    break
                results.append({
                    "run_id": run.pk,
                    "dataset": run.export_type or "cases",
                    # Per-row: was THIS ticket actually created? (older runs
                    # predate the field -> fall back to the run-level flag).
                    "applied": bool(pa.get("created", applied)),
                    "started_at": run.started_at,
                    "triggered_by": run.triggered_by,
                    "case_id": pa.get("case_id", ""),
                    "client_id": pa.get("client_id", ""),
                    "action": pa.get("action", ""),
                    "detail": pa.get("detail", ""),
                    "reason": pa.get("reason", ""),
                })
        totals["auth_changed_to"] = auth_to
        return Response({
            "totals": totals,
            "record_totals": record_totals,
            "results": results,
            "capped": self._RESULT_CAP,
            "any_applied": any_applied,
            "runs": run_options,
            "selected_run_id": int(run_id) if run_id.isdigit() else None,
        })


def _agent_dict(agent, case_count=None):
    d = {
        "id": str(agent.id),
        "user_id": str(agent.user_id),
        "name": agent.name,
        "email": agent.email,
        "work_title": agent.work_title,
        "status": agent.status,
        "is_us": agent.is_us,
        "originating_team": agent.originating_team,
    }
    if case_count is not None:
        d["case_count"] = case_count
    return d


def _case_counts_by_creator():
    """{created_by_id (lowercased str): number of imported cases}."""
    rows = (
        Case.objects.exclude(created_by_id__isnull=True)
        .values("created_by_id")
        .annotate(n=Count("case_id"))
    )
    return {str(r["created_by_id"]).lower(): r["n"] for r in rows}


class UniteUsAgentsView(PortalAPIView):
    """Settings > Import: the Unite Us agents allowlist.

    GET  — list every Unite Us agent (with how many imported cases each created).
    POST — add one by ``user_id`` (the Unite Us user id == Case.created_by_id),
           plus optional name / email / work_title.

    The cases import only accepts cases whose ``case_created_by_id`` is in this
    list (enforced only when the list is non-empty).
    """

    def get(self, request):
        counts = _case_counts_by_creator()
        agents = list(UniteUsAgent.objects.all())
        return Response(
            {
                "count": len(agents),
                "results": [
                    _agent_dict(a, counts.get(str(a.user_id).lower(), 0))
                    for a in agents
                ],
            }
        )

    def post(self, request):
        raw_user_id = (request.data.get("user_id") or "").strip()
        if not raw_user_id:
            return Response(
                {"user_id": "A Unite Us user_id is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        try:
            user_id = uuid.UUID(raw_user_id)
        except (ValueError, AttributeError, TypeError):
            return Response(
                {"user_id": "Must be a valid Unite Us user id (UUID)."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if UniteUsAgent.objects.filter(user_id=user_id).exists():
            return Response(
                {"user_id": "This Unite Us agent is already in the list."},
                status=http.HTTP_409_CONFLICT,
            )

        first = (request.data.get("first_name") or "").strip()
        last = (request.data.get("last_name") or "").strip()
        name = (request.data.get("name") or "").strip() or " ".join(
            p for p in [first, last] if p
        )
        agent = UniteUsAgent.objects.create(
            user_id=user_id,
            first_name=first,
            last_name=last,
            name=name,
            email=(request.data.get("email") or "").strip().lower(),
            work_title=(request.data.get("work_title") or "").strip(),
            status=(request.data.get("status") or "active").strip().lower(),
            is_us=bool(request.data.get("is_us", False)),
            originating_team=(
                (request.data.get("originating_team") or "").strip()
                or "Met Council Team"
            ),
        )
        counts = _case_counts_by_creator()
        return Response(
            _agent_dict(agent, counts.get(str(agent.user_id).lower(), 0)),
            status=http.HTTP_201_CREATED,
        )


class UniteUsAgentDetailView(PortalAPIView):
    """DELETE a Unite Us agent from the allowlist (remove from settings)."""

    def delete(self, request, agent_id):
        agent = UniteUsAgent.objects.filter(pk=agent_id).first()
        if agent is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        agent.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Settings > Import: automated Unite Us "Exports" (request -> poll -> import)
# ---------------------------------------------------------------------------
# Human labels for the export types the Request Export UI offers.
_UNITEUS_EXPORT_LABELS = {
    "clients": "Clients",
    "assessments": "Assessments",
    "cases": "Cases",
    "notes": "Notes",
    "screeningsv2": "Screenings V2.0",
}


def _export_summary(exp):
    run = exp.import_run
    return {
        "id": exp.pk,
        "export_id": exp.export_id,
        "export_type": exp.export_type,
        "export_type_label": _UNITEUS_EXPORT_LABELS.get(exp.export_type, exp.export_type),
        "importer_type": exp.importer_type,
        "start_date": exp.start_date,
        "end_date": exp.end_date,
        "unite_state": exp.unite_state,
        "status": exp.status,
        "status_label": exp.get_status_display(),
        "filename": exp.filename,
        "triggered_by": exp.triggered_by,
        "error_log": exp.error_log,
        "created_at": exp.created_at,
        "downloaded_at": exp.downloaded_at,
        "imported_at": exp.imported_at,
        # Link the import run so the UI can show counts/progress inline.
        "import_run": _run_summary(run) if run is not None else None,
    }


class UniteUsExportsView(PortalAPIView):
    """Settings > Import: automated Unite Us exports.

    GET  — supported types + this month's requested exports (with status).
    POST — request one or more exports (``export_types`` list or ``export_type``)
           for a date window (``start_date``/``end_date``, min 7 days). Each
           requested export is tracked and auto-downloaded + imported once Unite
           Us finishes generating it.
    """

    def get(self, request):
        from ..models import UniteUsExport

        month_start = timezone.localtime().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        exports = (
            UniteUsExport.objects.select_related("import_run")
            .filter(created_at__gte=month_start)
            .order_by("-created_at")
        )
        return Response({
            "supported_types": [
                {"value": v, "label": _UNITEUS_EXPORT_LABELS.get(v, v)}
                for v in uniteus_exports.SUPPORTED_EXPORT_TYPES
            ],
            "min_window_days": uniteus_exports.MIN_WINDOW_DAYS,
            "results": [_export_summary(e) for e in exports],
        })

    def post(self, request):
        from django.utils.dateparse import parse_date

        raw_types = request.data.get("export_types")
        if not raw_types:
            single = (request.data.get("export_type") or "").strip()
            raw_types = [single] if single else []
        export_types = [t for t in (raw_types or []) if t]
        if not export_types:
            return Response(
                {"detail": "Select at least one export type."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        bad = [t for t in export_types if t not in uniteus_exports.SUPPORTED_EXPORT_TYPES]
        if bad:
            return Response(
                {"detail": f"Unsupported export type(s): {', '.join(bad)}."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        start = parse_date((request.data.get("start_date") or "").strip())
        end = parse_date((request.data.get("end_date") or "").strip())
        if start is None or end is None:
            return Response(
                {"detail": "start_date and end_date (YYYY-MM-DD) are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if (end - start).days < uniteus_exports.MIN_WINDOW_DAYS:
            return Response(
                {"detail": f"The date range must be at least {uniteus_exports.MIN_WINDOW_DAYS} days."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        triggered_by = _triggered_by(request)
        created, errors = [], {}
        for etype in export_types:
            try:
                exp = uniteus_exports.request_export(
                    etype, start, end, triggered_by=triggered_by,
                )
                created.append(_export_summary(exp))
            except (ValueError, RuntimeError) as exc:
                errors[etype] = str(exc)
            except Exception as exc:  # noqa: BLE001 - surface API/transport errors cleanly
                errors[etype] = str(exc)

        # Kick the poller so requested exports start advancing without waiting
        # for the next beat tick (best-effort; the beat schedule covers it too).
        if created:
            try:
                poll_uniteus_exports.delay()
            except Exception:  # noqa: BLE001 - no broker in some envs; beat/cron still runs
                pass

        body = {"created": created, "errors": errors}
        if not created and errors:
            # Nothing requested -- surface WHY (e.g. expired token, no credential)
            # via ``detail`` so the UI shows it instead of a generic 400.
            body["detail"] = "; ".join(f"{t}: {m}" for t, m in errors.items())
        status_code = (
            http.HTTP_201_CREATED if created else http.HTTP_400_BAD_REQUEST
        )
        return Response(body, status=status_code)


class UniteUsExportPollView(PortalAPIView):
    """POST — trigger an immediate poll of pending Unite Us exports (the UI's
    "Refresh" button), so the user doesn't wait for the next scheduled tick."""

    def post(self, request):
        try:
            poll_uniteus_exports.delay()
            queued = True
        except Exception:  # noqa: BLE001 - no broker: fall back to inline
            uniteus_exports.poll_pending()
            queued = False
        return Response({"queued": queued}, status=http.HTTP_202_ACCEPTED)

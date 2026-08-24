"""Settings > Import: Proof-of-Delivery report upload (async S3 + Celery).

Mirrors the Unite Us CSV import flow (presign -> browser PUTs to S3 -> start ->
Celery), but each upload is tagged with the delivery COMPANY it came from (used
to stamp proofs + the order's delivery_company). See docs/proof_of_delivery_plan.md.
"""

import os

from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    DeliveryCompany, DeliveryCompanyStatus, ImportRun, ImportRunStatus,
)
from ..services import import_storage
from ..services.pod_import import POD_SOURCE
from ..tasks import process_pod_import
from .base import PortalAPIView, current_agent

_MAX_UPLOAD_BYTES = int(os.getenv("IMPORT_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024 * 1024)))
_MAX_UPLOAD_MB = _MAX_UPLOAD_BYTES // (1024 * 1024)


def _triggered_by(request):
    agent = current_agent(request)
    return f"agent:{agent.agent_code}" if agent and agent.agent_code else "manual"


def _pod_summary(run):
    stats = (run.stats or {}).get("delivery_pod") or {}
    return {
        "id": run.pk,
        "delivery_company_id": (run.stats or {}).get("delivery_company_id"),
        "original_filename": run.original_filename,
        "status": run.status,
        "status_label": run.get_status_display(),
        "triggered_by": run.triggered_by,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "processed": run.processed_count,
        "progress_total": run.progress_total,
        "progress_percent": (
            min(100, round(100 * (run.processed_count or 0) / run.progress_total))
            if run.progress_total else None
        ),
        "created": run.created_count,   # proofs created
        "updated": run.updated_count,   # orders updated
        "skipped": run.skipped_count,   # unmatched + deduped
        "errors": run.error_count,
        "error_log": run.error_log,
        "stats": stats,
    }


class PodImportRunsView(PortalAPIView):
    """GET recent POD import runs + the delivery-company options for the picker."""

    def get(self, request):
        month_start = timezone.localtime().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        runs = (
            ImportRun.objects.filter(source=POD_SOURCE, started_at__gte=month_start)
            .order_by("-started_at")
        )
        companies = [
            {"id": str(c.pk), "name": c.name}
            for c in DeliveryCompany.objects.filter(status=DeliveryCompanyStatus.ACTIVE)
        ]
        return Response({
            "async_uploads": import_storage.s3_enabled(),
            "max_upload_bytes": _MAX_UPLOAD_BYTES,
            "delivery_companies": companies,
            "results": [_pod_summary(r) for r in runs],
        })


class PodImportRunDetailView(PortalAPIView):
    """GET a single POD run -- polled by the UI for live progress/status."""

    def get(self, request, run_id):
        run = ImportRun.objects.filter(pk=run_id, source=POD_SOURCE).first()
        if run is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        return Response(_pod_summary(run))


class PodImportPresignView(PortalAPIView):
    """Step 1: validate + create a pending POD ImportRun (tagged with the
    delivery company) and return a presigned S3 PUT URL for the browser."""

    def post(self, request):
        if not import_storage.s3_enabled():
            return Response(
                {"detail": "Direct uploads are not configured (no S3 bucket)."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        company_id = (request.data.get("delivery_company_id") or "").strip()
        company = DeliveryCompany.objects.filter(pk=company_id).first() if company_id else None
        if company is None:
            return Response(
                {"delivery_company_id": "A valid delivery company is required."},
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
                {"file": f"File is too large (max {_MAX_UPLOAD_MB} MB)."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        key = import_storage.build_key(filename)
        run = ImportRun.objects.create(
            source=POD_SOURCE,
            status=ImportRunStatus.PENDING,
            triggered_by=_triggered_by(request),
            export_type="delivery_pod",
            file_key=key,
            original_filename=filename[:255],
            stats={"delivery_company_id": str(company.pk)},
        )
        try:
            upload_url = import_storage.presign_put(key, content_type="text/csv")
        except Exception as exc:  # noqa: BLE001
            run.status = ImportRunStatus.FAILED
            run.error_log = f"Could not presign upload: {exc}"
            run.save(update_fields=["status", "error_log"])
            return Response(
                {"detail": "Could not create an upload URL."},
                status=http.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {"run_id": run.pk, "upload_url": upload_url, "key": key,
             "content_type": "text/csv"},
            status=http.HTTP_201_CREATED,
        )


class PodImportStartView(PortalAPIView):
    """Step 2: after the browser finishes the S3 PUT, verify + enqueue."""

    def post(self, request, run_id):
        run = ImportRun.objects.filter(pk=run_id, source=POD_SOURCE).first()
        if run is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        if run.status != ImportRunStatus.PENDING:
            return Response(_pod_summary(run))
        if not run.file_key or not import_storage.object_exists(run.file_key):
            return Response(
                {"detail": "Uploaded file was not found in storage."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        process_pod_import.delay(run.pk)
        return Response(_pod_summary(run), status=http.HTTP_202_ACCEPTED)

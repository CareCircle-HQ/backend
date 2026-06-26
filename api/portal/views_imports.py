"""Settings > Import: manual Unite Us CSV upload (initial setup + backup)."""

from rest_framework import status as http
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from ..models import ImportRun
from ..services.csv_import import (
    CSV_SOURCE,
    SUPPORTED_EXPORT_TYPES,
    run_csv_import,
)
from .base import PortalAPIView, current_agent


def _run_summary(run):
    return {
        "id": run.pk,
        "source": run.source,
        # The stats dict is keyed by the imported dataset (clients/screenings/...);
        # surface it so the UI can label the run correctly.
        "dataset": next(iter((run.stats or {}).keys()), ""),
        "status": run.status,
        "status_label": run.get_status_display(),
        "triggered_by": run.triggered_by,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stats": run.stats,
        "processed": run.processed_count,
        "created": run.created_count,
        "updated": run.updated_count,
        "skipped": run.skipped_count,
        "errors": run.error_count,
        "error_log": run.error_log,
    }


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
        )
        status_code = (
            http.HTTP_200_OK if run.status == "completed" else http.HTTP_400_BAD_REQUEST
        )
        return Response(_run_summary(run), status=status_code)


class ImportRunsView(PortalAPIView):
    """GET the most recent CSV import runs (for the Settings > Import history)."""

    def get(self, request):
        runs = ImportRun.objects.filter(source=CSV_SOURCE).order_by("-started_at")[:20]
        return Response(
            {
                "supported_export_types": list(SUPPORTED_EXPORT_TYPES),
                "results": [_run_summary(r) for r in runs],
            }
        )

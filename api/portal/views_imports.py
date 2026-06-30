"""Settings > Import: manual Unite Us CSV upload (initial setup + backup) and
the Unite Us agents allowlist that gates which cases the import accepts."""

import uuid

from django.db.models import Count
from rest_framework import status as http
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from ..models import Case, ImportRun, UniteUsAgent
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

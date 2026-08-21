"""Agent Accountability dashboard -- per-agent (screener) productivity.

A single aggregate endpoint reporting, over a date window, how many:
  * Screenings          -- attributed to the agent who created that client's
                           internal-service case (screenings carry no submitter),
  * Eligibility Assessments -- by Assessment.provider_name (the submitter),
  * Internal Service cases  -- by Case.created_by_name (the creator),
each agent is responsible for. Agents are keyed by person name (the raw Unite Us
name that appears on cases + assessments).

The date window is a named ``period`` (today/week/month/last_month/year/all) or a
custom ``start``/``end`` range -- see ``resolve_window``.
"""

from collections import defaultdict

from django.db.models import Count
from rest_framework.response import Response

from ..models import Assessment, Case, CaseType, Screening
from .base import PortalAPIView, current_agent
from .views_dashboard import resolve_window


def _is_privileged(agent):
    """Accountability (per-agent performance) is a management view."""
    return bool(
        agent and (agent.group == "Management" or getattr(agent, "is_manager", False))
    )


class AgentAccountabilityView(PortalAPIView):
    """Per-screener productivity table (screenings / eligibility assessments /
    internal-service cases) over a date window. See module docstring."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Agent accountability access required."}, status=403
            )

        start, end = resolve_window(request)

        # --- Internal-service cases created, per agent (created_by_name) --------
        case_qs = Case.objects.filter(
            case_type=CaseType.INTERNAL_SERVICE
        ).exclude(created_by_name="")
        if start is not None:
            case_qs = case_qs.filter(
                case_created_at__date__gte=start, case_created_at__date__lte=end
            )
        cases_by = {
            r["created_by_name"]: r["n"]
            for r in case_qs.values("created_by_name").annotate(n=Count("case_id"))
        }

        # --- Eligibility assessments completed, per agent (provider_name) -------
        asmt_qs = Assessment.objects.exclude(provider_name="")
        if start is not None:
            asmt_qs = asmt_qs.filter(
                screen_created_at__date__gte=start, screen_created_at__date__lte=end
            )
        asmt_by = {
            r["provider_name"]: r["n"]
            for r in asmt_qs.values("provider_name").annotate(n=Count("assessment_id"))
        }

        # --- Screenings, attributed via the client's internal-service case ------
        # creator (screenings carry only the ORG as provider_name). Most-recent
        # internal-service case creator wins per client.
        client_creator = {}
        for r in (
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
            .exclude(created_by_name="")
            .exclude(client_id__isnull=True)
            .values("client_id", "created_by_name")
            .order_by("case_created_at")
        ):
            client_creator[r["client_id"]] = r["created_by_name"]  # last = most recent
        scr_qs = Screening.objects.exclude(client_id__isnull=True)
        if start is not None:
            scr_qs = scr_qs.filter(
                screen_created_at__date__gte=start, screen_created_at__date__lte=end
            )
        screen_by = defaultdict(int)
        for r in scr_qs.values("client_id").annotate(n=Count("enhanced_screen_id")):
            name = client_creator.get(r["client_id"])
            if name:
                screen_by[name] += r["n"]

        names = set(cases_by) | set(asmt_by) | set(screen_by)
        rows = [
            {
                "agent": n,
                "screenings": screen_by.get(n, 0),
                "assessments": asmt_by.get(n, 0),
                "internal_cases": cases_by.get(n, 0),
            }
            for n in names
        ]
        rows.sort(
            key=lambda r: (r["internal_cases"], r["assessments"], r["screenings"], r["agent"]),
            reverse=True,
        )
        return Response({
            "screeners": rows,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        })

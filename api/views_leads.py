"""Public eligibility-funnel lead capture.

Endpoints (registered under ``/api/leads/`` by the DefaultRouter):

    POST  /api/leads/              Create a lead — step 1 of the funnel.
                                   Public (no auth): this is the landing-page /
                                   mobile-app "Check My Eligibility" submission.
    PATCH /api/leads/<lead_id>/    Enrich a lead — step 2 (optional fields).
                                   Public, but keyed by the unguessable UUID
                                   returned on create, which acts as a
                                   capability token for the anonymous user.
    GET   /api/leads/              List leads — agent-authenticated (follow-up).
    GET   /api/leads/<lead_id>/    Retrieve a lead — agent-authenticated.

Listing/retrieving leads exposes PII, so those actions require an authenticated
agent; create/update are intentionally open so the public funnel can submit.
"""

from django.db.models import Q
from rest_framework import permissions, status as http, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Agent, Lead, LeadNote, ProgramMainCategory
from .serializers import LeadNoteSerializer, LeadSerializer


def _request_agent(request):
    """Resolve the requesting agent's DB row from the JWT principal (or None)."""
    agent_id = getattr(getattr(request, "user", None), "agent_id", None)
    return Agent.objects.filter(pk=agent_id).first() if agent_id else None


class LeadViewSet(viewsets.ModelViewSet):
    queryset = Lead.objects.all()
    serializer_class = LeadSerializer
    lookup_field = "lead_id"
    # No DELETE — leads are retained; use ``do_not_contact`` to opt a lead out.
    http_method_names = ["get", "post", "patch", "put", "head", "options"]

    # Actions reachable by the anonymous public funnel. Everything else (list,
    # retrieve, set_status, notes) is restricted to authenticated agents.
    PUBLIC_ACTIONS = {"create", "update", "partial_update"}

    def get_permissions(self):
        if self.action in self.PUBLIC_ACTIONS:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def perform_create(self, serializer):
        # Auto-assign a new lead to the agent who created it (e.g. a screener
        # capturing a lead from the extension). The public funnel is anonymous,
        # so this only applies when an agent JWT is present.
        agent = _request_agent(self.request)
        if agent and not serializer.validated_data.get("assigned_to"):
            serializer.save(assigned_to=agent)
        else:
            serializer.save()

    def get_queryset(self):
        qs = (
            Lead.objects.select_related("assigned_to", "converted_client")
            .prefetch_related("interested_programs", "notes")
            .order_by("-created_at")
        )
        # Filtering only applies to the agent-facing list (follow-up queue).
        if self.action != "list":
            return qs
        p = self.request.query_params

        # ?mine=true -> only leads assigned to the requesting agent (from the JWT).
        mine = (p.get("mine") or "").strip().lower()
        if mine in ("1", "true", "yes"):
            agent_id = getattr(getattr(self.request, "user", None), "agent_id", None)
            qs = qs.filter(assigned_to_id=agent_id) if agent_id else qs.none()

        status_val = (p.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            qs = qs.filter(status=status_val)
        else:
            # Default view hides terminal/closed-out leads; pick a specific
            # status to see Close / Lost / Not Eligible.
            qs = qs.exclude(
                status__in=[
                    Lead.Status.CLOSED,
                    Lead.Status.LOST,
                    Lead.Status.NOT_ELIGIBLE,
                ]
            )

        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(phone_number__icontains=search)
                | Q(email__icontains=search)
                | Q(zip_code__icontains=search)
            )
        return qs

    @action(detail=True, methods=["patch"], url_path="status")
    def set_status(self, request, lead_id=None):
        """Agent-only: change a lead's follow-up status."""
        lead = self.get_object()
        new_status = (request.data.get("status") or "").strip()
        if new_status not in Lead.Status.values:
            return Response(
                {"status": "Invalid status."}, status=http.HTTP_400_BAD_REQUEST
            )
        lead.status = new_status
        lead.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(lead).data)

    @action(detail=True, methods=["get", "post"])
    def notes(self, request, lead_id=None):
        """Agent-only: list a lead's notes (GET) or add one (POST)."""
        lead = self.get_object()
        if request.method == "POST":
            body = (request.data.get("body") or "").strip()
            if not body:
                return Response(
                    {"body": "Note text is required."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            agent = _request_agent(request)
            author_name = (
                agent.name
                if agent
                else getattr(getattr(request, "user", None), "name", "") or ""
            )
            LeadNote.objects.create(
                lead=lead, author=agent, author_name=author_name, body=body
            )
        notes = lead.notes.all()
        return Response(LeadNoteSerializer(notes, many=True).data)

    @action(detail=False, methods=["get"], url_path="program-categories")
    def program_categories(self, request):
        """Agent-only: program main categories for the lead interest picker.

        Mirrors the portal endpoint but is reachable by any authenticated agent
        (the Leads tab is available to screeners, who are not portal agents).
        """
        cats = ProgramMainCategory.objects.order_by("name").values("id", "name")
        return Response(list(cats))

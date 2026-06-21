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

from rest_framework import permissions, viewsets

from .models import Lead
from .serializers import LeadSerializer


class LeadViewSet(viewsets.ModelViewSet):
    queryset = Lead.objects.all()
    serializer_class = LeadSerializer
    lookup_field = "lead_id"
    # No DELETE — leads are retained; use ``do_not_contact`` to opt a lead out.
    http_method_names = ["get", "post", "patch", "put", "head", "options"]

    # Actions reachable by the anonymous public funnel. Everything else (list,
    # retrieve) is restricted to authenticated agents.
    PUBLIC_ACTIONS = {"create", "update", "partial_update"}

    def get_permissions(self):
        if self.action in self.PUBLIC_ACTIONS:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

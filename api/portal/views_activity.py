"""Settings > Activity Log: a cross-client admin feed of the client timeline.

The same `TimelineEvent` rows that power a member's History tab and a case's
"Case history", but unscoped -- every client -- with filters (event type,
source, actor, date range, free-text) so an admin can see what happened across
the whole system and click through to the member / ticket / case.
"""

from django.db.models import Q, TextField
from django.db.models.functions import Cast
from rest_framework.response import Response

from ..models import TimelineEvent, TimelineEventType
from . import serializers as s
from .base import PortalAPIView, PortalGenericAPIView


class ActivityLogView(PortalGenericAPIView):
    """Paginated, filterable cross-client timeline feed (newest first)."""

    serializer_class = s.ActivityEventSerializer

    def get(self, request):
        qs = (
            TimelineEvent.objects.select_related("client", "content_type")
            .order_by("-occurred_at", "-created_at")
        )
        p = request.query_params

        event_type = (p.get("event_type") or "").strip()
        if event_type:
            qs = qs.filter(event_type=event_type)

        source = (p.get("source") or "").strip()
        if source:
            qs = qs.filter(source=source)

        actor = (p.get("actor") or "").strip()
        if actor:
            qs = qs.filter(actor__icontains=actor)

        # One search box: member name, event text, OR client id (full/partial --
        # the UUID is cast to text so a fragment matches).
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.annotate(
                _cid_text=Cast("client__client_id", TextField())
            ).filter(
                Q(client__first_name__icontains=search)
                | Q(client__last_name__icontains=search)
                | Q(title__icontains=search)
                | Q(subtitle__icontains=search)
                | Q(_cid_text__icontains=search)
            )

        date_from = (p.get("from") or "").strip()
        if date_from:
            qs = qs.filter(occurred_at__date__gte=date_from)
        date_to = (p.get("to") or "").strip()
        if date_to:
            qs = qs.filter(occurred_at__date__lte=date_to)

        page = self.paginate_queryset(qs)
        data = self.get_serializer(page, many=True).data
        return self.get_paginated_response(data)


class ActivityFiltersView(PortalAPIView):
    """Values to populate the Activity Log filter dropdowns."""

    def get(self, request):
        return Response({
            "event_types": [
                {"value": v, "label": label}
                for v, label in TimelineEventType.choices
            ],
            "sources": [
                {"value": "import", "label": "Import"},
                {"value": "extension", "label": "Agent (extension)"},
                {"value": "system", "label": "System"},
                {"value": "crm", "label": "CRM"},
                {"value": "admin", "label": "Admin"},
            ],
        })

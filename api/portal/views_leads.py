"""Agent-facing lead capture + listing for the support portal.

Agents can manually open a lead from the Work Queue ("New Lead") and browse
captured leads from the Leads page. This reuses the public-funnel
:class:`~api.serializers.LeadSerializer` but, unlike the open ``/api/leads/``
funnel endpoint, requires an authenticated portal agent.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..models import Agent, Lead, LeadNote, ProgramMainCategory
from ..serializers import LeadNoteSerializer, LeadSerializer
from . import serializers as s
from .base import PortalAPIView, PortalGenericAPIView, current_agent


class PortalLeadsView(PortalGenericAPIView):
    serializer_class = LeadSerializer

    def get_queryset(self):
        # Lead.Meta orders by -created_at.
        qs = Lead.objects.select_related("assigned_to", "converted_client").prefetch_related(
            "interested_programs", "notes"
        )
        p = self.request.query_params
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(phone_number__icontains=search)
                | Q(email__icontains=search)
                | Q(zip_code__icontains=search)
                | Q(medicaid_id__icontains=search)
            )
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
        return qs

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def post(self, request):
        ser = LeadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        lead = ser.save()
        return Response(LeadSerializer(lead).data, status=http.HTTP_201_CREATED)


def _load_lead(lead_id):
    return get_object_or_404(
        Lead.objects.select_related("assigned_to", "converted_client").prefetch_related(
            "interested_programs", "notes"
        ),
        pk=lead_id,
    )


class PortalLeadDetailView(PortalAPIView):
    """Retrieve / update a single lead (e.g. assign a screener, link the
    converted client, change status)."""

    def get(self, request, lead_id):
        return Response(LeadSerializer(_load_lead(lead_id)).data)

    def patch(self, request, lead_id):
        lead = _load_lead(lead_id)
        # ``status`` is read-only on LeadSerializer (managed internally), so
        # handle status transitions explicitly here.
        new_status = request.data.get("status")
        if new_status is not None:
            if new_status not in Lead.Status.values:
                return Response(
                    {"status": "Invalid status."}, status=http.HTTP_400_BAD_REQUEST
                )
            lead.status = new_status
            lead.save(update_fields=["status", "updated_at"])
        # Apply any other writable fields (e.g. assigned_to) via the serializer.
        other = {k: v for k, v in request.data.items() if k != "status"}
        if other:
            ser = LeadSerializer(lead, data=other, partial=True)
            ser.is_valid(raise_exception=True)
            ser.save()
        return Response(LeadSerializer(_load_lead(lead_id)).data)


class PortalLeadNotesView(PortalAPIView):
    """List (GET) or add (POST) follow-up notes on a lead."""

    def get(self, request, lead_id):
        lead = _load_lead(lead_id)
        return Response(LeadNoteSerializer(lead.notes.all(), many=True).data)

    def post(self, request, lead_id):
        lead = _load_lead(lead_id)
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response(
                {"body": "Note text is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        agent = current_agent(request)
        LeadNote.objects.create(
            lead=lead,
            author=agent,
            author_name=(agent.name if agent else ""),
            body=body,
        )
        return Response(
            LeadNoteSerializer(lead.notes.all(), many=True).data,
            status=http.HTTP_201_CREATED,
        )


class PortalScreenersView(PortalAPIView):
    """Active screeners, for the lead-assignment dropdown."""

    def get(self, request):
        screeners = Agent.objects.filter(
            status="Active", group="Screeners"
        ).order_by("name")
        return Response(s.PortalAgentSerializer(screeners, many=True).data)


class PortalProgramCategoriesView(PortalAPIView):
    """Program main categories, for the lead "programs of interest" dropdown."""

    def get(self, request):
        cats = ProgramMainCategory.objects.order_by("name").values("id", "name")
        return Response(list(cats))

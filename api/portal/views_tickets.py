"""Work queue (global tickets) + ticket detail/status/notes + agents list."""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response

from ..models import Agent, Ticket, TicketNote
from .base import PortalAPIView, PortalGenericAPIView, current_agent
from .permissions import PORTAL_ALLOWED_GROUPS
from . import serializers as s

TICKET_PREFETCH = ("notes",)
TICKET_SELECT = ("assigned_to", "client", "case")

# open -> in_progress -> resolved -> open
STATUS_CYCLE = {"open": "in_progress", "in_progress": "resolved", "resolved": "open"}


class WorkQueueView(PortalGenericAPIView):
    serializer_class = s.PortalTicketSerializer

    def get_queryset(self):
        qs = (
            Ticket.objects.all()
            .select_related(*TICKET_SELECT)
            .prefetch_related(*TICKET_PREFETCH)
        )
        p = self.request.query_params
        search = (p.get("search") or "").strip()
        if search:
            cond = (
                Q(reason__icontains=search)
                | Q(client__first_name__icontains=search)
                | Q(client__last_name__icontains=search)
            )
            digits = "".join(c for c in search if c.isdigit())
            if digits:
                cond |= Q(pk=int(digits)) if digits.isdigit() else Q()
            qs = qs.filter(cond)
        status_val = (p.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            qs = qs.filter(status=status_val.replace("-", "_"))
        severity = (p.get("severity") or "").strip()
        if severity and severity.lower() not in ("all", "all severities"):
            qs = qs.filter(severity=severity)
        type_val = (p.get("type") or "").strip()
        if type_val and type_val.lower() not in ("all", "all types"):
            qs = qs.filter(type=type_val)
        return qs.distinct()

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def post(self, request):
        ser = s.PortalTicketCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        ticket = Ticket.objects.create(
            type=data["type"],
            severity=data.get("severity", "medium"),
            reason=data["reason"],
            client_id=data.get("client_id"),
            case_id=data.get("case_id"),
            assigned_to_id=data.get("assignee_id"),
        )
        ticket = (
            Ticket.objects.select_related(*TICKET_SELECT)
            .prefetch_related(*TICKET_PREFETCH)
            .get(pk=ticket.pk)
        )
        return Response(
            s.PortalTicketSerializer(ticket).data, status=http.HTTP_201_CREATED
        )


class TicketsStatsView(PortalAPIView):
    def get(self, request):
        qs = Ticket.objects.all()
        return Response(
            {
                "open": qs.filter(status="open").count(),
                "in_progress": qs.filter(status="in_progress").count(),
                "resolved": qs.filter(status="resolved").count(),
                "high": qs.filter(severity="high").exclude(status="resolved").count(),
            }
        )


def _load_ticket(pk):
    return get_object_or_404(
        Ticket.objects.select_related(*TICKET_SELECT).prefetch_related(*TICKET_PREFETCH),
        pk=pk,
    )


class TicketDetailView(PortalAPIView):
    def get(self, request, ticket_id):
        return Response(s.PortalTicketSerializer(_load_ticket(ticket_id)).data)

    def patch(self, request, ticket_id):
        ticket = _load_ticket(ticket_id)
        new_status = (request.data.get("status") or "").replace("-", "_").strip()
        if not new_status:
            new_status = STATUS_CYCLE.get(ticket.status, "open")
        if new_status not in STATUS_CYCLE:
            return Response(
                {"error": "Invalid status."}, status=http.HTTP_400_BAD_REQUEST
            )
        ticket.status = new_status
        if new_status == "resolved":
            ticket.resolved_at = timezone.now()
            agent = current_agent(request)
            ticket.resolved_by = (
                f"agent:{agent.agent_code}" if agent and agent.agent_code
                else (agent.name if agent else "")
            )
        else:
            ticket.resolved_at = None
            ticket.resolved_by = ""
        ticket.save(update_fields=["status", "resolved_at", "resolved_by", "updated_at"])
        return Response(s.PortalTicketSerializer(ticket).data)


class TicketNotesView(PortalAPIView):
    def post(self, request, ticket_id):
        ticket = get_object_or_404(Ticket, pk=ticket_id)
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response(
                {"error": "Note body is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        agent = current_agent(request)
        note = TicketNote.objects.create(
            ticket=ticket,
            author_agent=agent,
            author_name=agent.name if agent else "",
            body=body,
        )
        return Response(
            s.PortalTicketNoteSerializer(note).data, status=http.HTTP_201_CREATED
        )


class AgentsListView(PortalAPIView):
    def get(self, request):
        agents = Agent.objects.filter(
            status="Active", group__in=PORTAL_ALLOWED_GROUPS
        ).order_by("name")
        return Response(s.PortalAgentSerializer(agents, many=True).data)

"""Work queue (global tickets) + ticket detail/status/notes + agents list."""

from datetime import datetime

from django.db.models import Case as DBCase, IntegerField, Q, When
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response

from ..models import Agent, Ticket, TicketNote, TicketOrigin, TicketType
from ..services import timeline
from .base import PortalAPIView, PortalGenericAPIView, current_agent
from . import serializers as s

TICKET_PREFETCH = ("notes", "client__household_membership__household__members")
TICKET_SELECT = ("assigned_to", "client", "case", "type")

# open -> in_progress -> resolved -> open
STATUS_CYCLE = {"open": "in_progress", "in_progress": "resolved", "resolved": "open"}


def _parse_date(value):
    """Parse a YYYY-MM-DD (or common US) date string; None on failure/blank."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


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
            qs = qs.filter(type__code=type_val)
        origin_val = (p.get("origin") or "").strip()
        if origin_val and origin_val.lower() not in ("all", "all origins"):
            qs = qs.filter(origin=origin_val)
        # VIP filter: ?vip=1 restricts to VIP-flagged tickets.
        vip_val = (p.get("vip") or "").strip().lower()
        if vip_val in ("1", "true", "yes"):
            qs = qs.filter(vip=True)
        source_val = (p.get("source") or "").strip()
        if source_val and source_val.lower() not in ("all", "all sources"):
            qs = qs.filter(source=source_val)
        mine = (p.get("mine") or "").strip().lower()
        if mine in ("1", "true", "yes"):
            agent = current_agent(self.request)
            qs = qs.filter(assigned_to=agent) if agent else qs.none()
        # Filter by a specific assignee (manager view: inspect one agent's queue),
        # or the "unassigned" sentinel to surface tickets nobody owns yet.
        assignee_val = (p.get("assignee") or "").strip()
        if assignee_val.lower() == "unassigned":
            qs = qs.filter(assigned_to__isnull=True)
        elif assignee_val:
            qs = qs.filter(assigned_to_id=assignee_val)
        # Date-created range (inclusive). ``created_at`` is a datetime; ``__date``
        # extraction uses the active timezone (America/New_York), so the window
        # matches the local calendar day the agent selects.
        created_from = _parse_date(p.get("created_from"))
        if created_from:
            qs = qs.filter(created_at__date__gte=created_from)
        created_to = _parse_date(p.get("created_to"))
        if created_to:
            qs = qs.filter(created_at__date__lte=created_to)
        # Stable ordering is REQUIRED for correct pagination (without an ORDER BY
        # the page contents are arbitrary and can repeat/skip across pages).
        # Default newest-first; honor the list's recent/oldest sort toggle.
        ordering = (p.get("ordering") or "-created_at").strip()
        if ordering not in ("created_at", "-created_at"):
            ordering = "-created_at"
        return qs.distinct().order_by(ordering, "-pk")

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def post(self, request):
        ser = s.PortalTicketCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        # Auto-link the member's governing case when a member is known but the
        # agent didn't pick a specific case, so the ticket also points at the
        # case it concerns.
        case_id = data.get("case_id")
        client_id = data.get("client_id")
        if not case_id and client_id:
            from ..models import Client
            from ..services.tickets import governing_case_for_client

            gov = governing_case_for_client(
                Client.objects.filter(pk=client_id).first()
            )
            if gov is not None:
                case_id = gov.pk
        agent = current_agent(request)
        ticket = Ticket.objects.create(
            type=data["type"],
            severity=data.get("severity", "medium"),
            source=data.get("source", ""),
            origin=TicketOrigin.AGENT,
            vip=data.get("vip", False),
            reason=data["reason"],
            client_id=client_id,
            case_id=case_id,
            assigned_to_id=data.get("assignee_id"),
            created_by=agent,
            created_by_label=(agent.name if agent else ""),
        )
        from ..services.tickets import log_ticket_activity
        from ..models import TicketActivityAction

        log_ticket_activity(
            ticket, TicketActivityAction.CREATED, actor_agent=agent,
            actor_label=(agent.name if agent else ""), detail="Ticket created.",
        )
        if ticket.assigned_to_id:
            log_ticket_activity(
                ticket, TicketActivityAction.ASSIGNED, actor_agent=agent,
                actor_label=(agent.name if agent else ""),
                detail=f"Assigned to {ticket.assigned_to.name}.",
                metadata={"assignee_id": str(ticket.assigned_to_id)},
            )
        ticket = (
            Ticket.objects.select_related(*TICKET_SELECT)
            .prefetch_related(*TICKET_PREFETCH)
            .get(pk=ticket.pk)
        )
        # Mirror the manual ticket onto the client's history timeline.
        if ticket.client_id:
            agent = current_agent(request)
            try:
                timeline.event_for_ticket_created(
                    ticket,
                    actor=(f"agent:{agent.code}" if agent and agent.code else ""),
                )
            except Exception:  # never let history-logging break ticket creation
                pass
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
        update_fields = []
        actor = current_agent(request)
        actor_label = (actor.name if actor else "")
        prev_assignee_id = ticket.assigned_to_id
        prev_status = ticket.status

        # Reassignment: the presence of the `assignee_id` key signals intent to
        # change the assignee (an empty/null value unassigns). Done independently
        # of status, so reassigning never advances the ticket's status.
        if "assignee_id" in request.data:
            aid = request.data.get("assignee_id") or None
            if aid:
                agent = Agent.objects.filter(
                    pk=aid, status="Active", group__in=ASSIGNABLE_GROUPS
                ).first()
                if agent is None:
                    return Response(
                        {"error": "Unknown or non-assignable agent."},
                        status=http.HTTP_400_BAD_REQUEST,
                    )
                ticket.assigned_to = agent
            else:
                ticket.assigned_to = None
            update_fields.append("assigned_to")

        # Status change / cycle. Applied when a status is explicitly provided, or
        # when this is a bare PATCH with no reassignment (preserves the list's
        # status-cycle button, which sends an empty body).
        if "status" in request.data or "assignee_id" not in request.data:
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
            update_fields += ["status", "resolved_at", "resolved_by"]

        update_fields.append("updated_at")
        ticket.save(update_fields=update_fields)

        # Record the activity feed entries for what actually changed.
        from ..models import TicketActivityAction
        from ..services.tickets import log_ticket_activity

        if "assigned_to" in update_fields and ticket.assigned_to_id != prev_assignee_id:
            if ticket.assigned_to_id:
                log_ticket_activity(
                    ticket, TicketActivityAction.ASSIGNED, actor_agent=actor,
                    actor_label=actor_label,
                    detail=f"Assigned to {ticket.assigned_to.name}.",
                    metadata={"assignee_id": str(ticket.assigned_to_id)},
                )
            else:
                log_ticket_activity(
                    ticket, TicketActivityAction.UNASSIGNED, actor_agent=actor,
                    actor_label=actor_label, detail="Unassigned.",
                )
        if "status" in update_fields and ticket.status != prev_status:
            if ticket.status == "resolved":
                action = TicketActivityAction.RESOLVED
            elif prev_status == "resolved":
                action = TicketActivityAction.REOPENED
            else:
                action = TicketActivityAction.STATUS_CHANGED
            log_ticket_activity(
                ticket, action, actor_agent=actor, actor_label=actor_label,
                detail=(
                    f"Status: {prev_status.replace('_', ' ').title()} \u2192 "
                    f"{ticket.status.replace('_', ' ').title()}"
                ),
                metadata={"from": prev_status, "to": ticket.status},
            )

        ticket = _load_ticket(ticket.pk)
        return Response(s.PortalTicketSerializer(ticket).data)


class TicketActivityView(PortalAPIView):
    """GET a ticket's activity/history feed (created, assigned, status changes,
    notes, resolved, ...), oldest-first, so the Work Queue can show a timestamped
    history of what happened to the ticket and who did it."""

    def get(self, request, ticket_id):
        ticket = get_object_or_404(Ticket, pk=ticket_id)
        activities = (
            ticket.activities.select_related("actor_agent").order_by("created_at", "pk")
        )
        return Response(s.PortalTicketActivitySerializer(activities, many=True).data)


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
        from ..models import TicketActivityAction
        from ..services.tickets import log_ticket_activity

        excerpt = body if len(body) <= 140 else body[:139] + "\u2026"
        log_ticket_activity(
            ticket, TicketActivityAction.NOTE_ADDED, actor_agent=agent,
            actor_label=(agent.name if agent else ""), detail=excerpt,
        )
        return Response(
            s.PortalTicketNoteSerializer(note).data, status=http.HTTP_201_CREATED
        )


class TicketTypesListView(PortalAPIView):
    """Active ticket types for the New-Ticket type picker (loaded from the DB)."""

    def get(self, request):
        types = TicketType.objects.filter(is_active=True).order_by("label")
        return Response(s.PortalTicketTypeSerializer(types, many=True).data)


# Groups assignable to a ticket, in display order (CS first, then Management).
ASSIGNABLE_GROUPS = ["CS", "Management"]


class AgentsListView(PortalAPIView):
    def get(self, request):
        agents = (
            Agent.objects.filter(status="Active", group__in=ASSIGNABLE_GROUPS)
            .order_by(
                DBCase(
                    When(group="CS", then=0),
                    default=1,
                    output_field=IntegerField(),
                ),
                "name",
            )
        )
        return Response(s.PortalAgentSerializer(agents, many=True).data)

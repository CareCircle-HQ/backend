import logging
import uuid

from django.db.models import Q
from rest_framework import generics, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.decorators import action
from rest_framework import status

from .models import (
    Assessment,
    Case,
    Client,
    ContractedService,
    HouseholdMember,
    Program,
    Provider,
    Screening,
    TimelineEvent,
)
from .serializers import (
    AssessmentSerializer,
    CaseSerializer,
    ClientSerializer,
    ContractedServiceSerializer,
    HouseholdSerializer,
    ProgramSerializer,
    ProviderSerializer,
    RegisterSerializer,
    ScreeningSerializer,
    TimelineEventSerializer,
    UserSerializer,
    ensure_household_with_primary,
)
# TEMPORARY external-CRM mirror; remove with the api/integrations package.
from .integrations import ghl
from .services import timeline

logger = logging.getLogger(__name__)


def _agent_actor(request):
    """Attribution string for the authenticated agent, e.g. 'agent:355'."""
    code = getattr(getattr(request, "user", None), "agent_code", None)
    return f"agent:{code}" if code else ""


def _safe_timeline(builder, obj, request):
    """Emit a timeline event, never letting a failure break the API write."""
    try:
        builder(obj, actor=_agent_actor(request))
    except Exception:  # noqa: BLE001
        logger.exception("timeline emit failed for %s", type(obj).__name__)


class RegisterView(generics.CreateAPIView):
    """Public endpoint to create a new user."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeView(APIView):
    """Return the currently authenticated user (requires a valid JWT)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class HealthView(APIView):
    """Simple public health check."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({"status": "ok"})


class BulkUpsertMixin:
    """Adds a /bulk/ action accepting a list of records for batch upsert.

    Each item is upserted independently; per-item errors are collected and
    returned without failing the whole batch.
    """

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        items = request.data
        if not isinstance(items, list):
            return Response(
                {"detail": "Expected a JSON list of records."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        created, errors = [], []
        for index, item in enumerate(items):
            serializer = self.get_serializer(data=item)
            if serializer.is_valid():
                obj = serializer.save()
                self.post_upsert(obj)
                created.append(str(obj.pk))
            else:
                errors.append({"index": index, "errors": serializer.errors})
        return Response(
            {
                "received": len(items),
                "succeeded": len(created),
                "failed": len(errors),
                "ids": created,
                "errors": errors,
            },
            status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_200_OK,
        )

    def post_upsert(self, obj):
        """Hook called after a successful bulk upsert. No-op by default."""
        return None


class ClientViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for clients (keyed on source client_id UUID)."""

    queryset = Client.objects.all().prefetch_related(
        "addresses", "insurances", "social_care_coverages", "military_profile"
    )
    serializer_class = ClientSerializer

    def _agent_save_kwargs(self):
        """Stamp the authenticated agent's real code + full name onto the client.

        The extension sends the agent's NAME in ``agent_code``; the canonical
        code and name live on the agent JWT (``request.user.agent_code`` /
        ``request.user.name``). Override them so downstream (GHL Agent Code /
        Assigned Agent) resolves reliably.
        """
        user = self.request.user
        kwargs = {}
        code = getattr(user, "agent_code", None)
        if code:
            kwargs["agent_code"] = code
        name = getattr(user, "name", None)
        if name:
            kwargs["agent_name"] = name
        return kwargs

    # --- TEMPORARY: mirror the client to the external GHL CRM on save. The
    # sync is best-effort and never raises; remove these three hooks (and the
    # api/integrations package) when the external CRM is retired. ---
    def perform_create(self, serializer):
        serializer.save(**self._agent_save_kwargs())
        ghl.sync_client(serializer.instance)
        _safe_timeline(timeline.event_for_consent, serializer.instance, self.request)

    def perform_update(self, serializer):
        serializer.save(**self._agent_save_kwargs())
        ghl.sync_client(serializer.instance)
        _safe_timeline(timeline.event_for_consent, serializer.instance, self.request)

    def post_upsert(self, obj):
        # Bulk path: stamp the agent code + name from the JWT if available.
        user = self.request.user
        updates = []
        code = getattr(user, "agent_code", None)
        if code and obj.agent_code != code:
            obj.agent_code = code
            updates.append("agent_code")
        name = getattr(user, "name", None)
        if name and obj.agent_name != name:
            obj.agent_name = name
            updates.append("agent_name")
        if updates:
            obj.save(update_fields=updates)
        ghl.sync_client(obj)
        _safe_timeline(timeline.event_for_consent, obj, self.request)

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """Central client history: all domain events newest-first, plus renewal
        grouping metadata for the dashboard's "Renewal #N" section headers."""
        client = self.get_object()
        events = list(
            TimelineEvent.objects.filter(client=client)
            .select_related("content_type", "enrollment")
            .order_by("-occurred_at", "-created_at")
        )
        by_cycle = {}
        for ev in events:
            by_cycle.setdefault(ev.renewal_number, []).append(ev)
        renewals = []
        for num in sorted(by_cycle, reverse=True):
            if num < 2:  # cycle 1 is the initial (ungrouped) timeline
                continue
            dates = [e.occurred_at for e in by_cycle[num] if e.occurred_at]
            renewals.append({
                "renewal_number": num,
                "label": f"Renewal #{num}",
                "period_start": min(dates).date().isoformat() if dates else None,
                "period_end": max(dates).date().isoformat() if dates else None,
                "count": len(by_cycle[num]),
            })
        return Response({
            "client_id": str(client.pk),
            "renewals": renewals,
            "results": TimelineEventSerializer(events, many=True).data,
        })

    @action(detail=False, methods=["get"])
    def search(self, request):
        """Find existing clients by member ID (client UUID) or by Medicaid /
        insurance member ID (external_member_id). Used by the household member
        picker. Returns lightweight rows, not the full client serializer."""
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response([])

        filters = (
            Q(insurances__external_member_id__icontains=q)
            | Q(social_care_coverages__external_member_id__icontains=q)
        )
        # client_id is a UUID column: only match it when q parses as a UUID.
        try:
            filters |= Q(client_id=uuid.UUID(q))
        except (ValueError, AttributeError, TypeError):
            pass

        qs = (
            Client.objects.filter(filters)
            .distinct()
            .prefetch_related("insurances", "social_care_coverages")[:25]
        )

        results = []
        for c in qs:
            member_ids = sorted({
                mid
                for src in (c.insurances.all(), c.social_care_coverages.all())
                for mid in (x.external_member_id for x in src)
                if mid
            })
            results.append({
                "client_id": str(c.client_id),
                "first_name": c.first_name,
                "last_name": c.last_name,
                "date_of_birth": c.date_of_birth.isoformat() if c.date_of_birth else None,
                "member_ids": member_ids,
                "in_household": HouseholdMember.objects.filter(client=c).exists(),
            })
        return Response(results)

    def _household_response(self, client):
        household = ensure_household_with_primary(client)
        data = HouseholdSerializer(household).data
        data["max_members"] = client.total_family_members or 1
        return Response(data)

    @action(detail=True, methods=["get"])
    def household(self, request, pk=None):
        """Get-or-create this client's household (with the client as primary)
        and return it with its members and the max member cap."""
        return self._household_response(self.get_object())

    @action(detail=True, methods=["post"], url_path="household/add")
    def household_add(self, request, pk=None):
        """Add an existing client to this client's household. Enforces the
        family-size cap and the one-household-per-client rule."""
        primary = self.get_object()
        member_id = request.data.get("client_id")
        if not member_id:
            return Response(
                {"detail": "client_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        household = ensure_household_with_primary(primary)

        # Idempotent: already a member of THIS household -> just return it.
        if household.members.filter(client_id=member_id).exists():
            return self._household_response(primary)

        max_members = primary.total_family_members or 1
        if household.members.count() >= max_members:
            return Response(
                {"detail": f"Household is full ({max_members} member(s) allowed)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            member_client = Client.objects.get(pk=member_id)
        except (Client.DoesNotExist, ValueError):
            return Response(
                {"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND
            )
        if HouseholdMember.objects.filter(client=member_client).exists():
            return Response(
                {"detail": "Client already belongs to a household."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        HouseholdMember.objects.create(
            household=household, client=member_client, is_primary=False
        )
        return self._household_response(primary)

    @action(detail=True, methods=["post"], url_path="household/remove")
    def household_remove(self, request, pk=None):
        """Remove a member from this client's household. The primary member
        cannot be removed."""
        primary = self.get_object()
        member_id = request.data.get("client_id")
        household = ensure_household_with_primary(primary)
        member = household.members.filter(client_id=member_id).first()
        if member is None:
            return Response(
                {"detail": "Not a member of this household."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if member.is_primary:
            return Response(
                {"detail": "The primary member cannot be removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        member.delete()
        return self._household_response(primary)


class CaseViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for cases (keyed on source case_id UUID)."""

    queryset = Case.objects.select_related(
        "client", "provider", "originating_provider", "program"
    )
    serializer_class = CaseSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # --- TEMPORARY: mirror the case to the external GHL CRM as an opportunity.
    def perform_create(self, serializer):
        serializer.save()
        ghl.sync_case(serializer.instance)
        _safe_timeline(timeline.event_for_case, serializer.instance, self.request)

    def perform_update(self, serializer):
        serializer.save()
        ghl.sync_case(serializer.instance)
        _safe_timeline(timeline.event_for_case, serializer.instance, self.request)

    def post_upsert(self, obj):
        ghl.sync_case(obj)
        _safe_timeline(timeline.event_for_case, obj, self.request)


class ContractedServiceViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for contracted services (keyed on provided_service UUID).

    Filterable by ``?case=<uuid>`` or ``?client=<uuid>`` (via the parent case).
    """

    queryset = ContractedService.objects.select_related("case")
    serializer_class = ContractedServiceSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        case = self.request.query_params.get("case")
        if case:
            qs = qs.filter(case_id=case)
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(case__client_id=client)
        return qs


class ScreeningViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for screenings (keyed on enhanced_screen_id UUID)."""

    queryset = Screening.objects.select_related("client")
    serializer_class = ScreeningSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # --- TEMPORARY: mirror the screening to the external GHL CRM as an opportunity.
    def perform_create(self, serializer):
        serializer.save()
        ghl.sync_screening(serializer.instance)
        _safe_timeline(timeline.event_for_screening, serializer.instance, self.request)

    def perform_update(self, serializer):
        serializer.save()
        ghl.sync_screening(serializer.instance)
        _safe_timeline(timeline.event_for_screening, serializer.instance, self.request)

    def post_upsert(self, obj):
        ghl.sync_screening(obj)
        _safe_timeline(timeline.event_for_screening, obj, self.request)


class AssessmentViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for assessments (keyed on assessment_id UUID)."""

    queryset = Assessment.objects.select_related("client")
    serializer_class = AssessmentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # NOTE: Assessments are intentionally NOT mirrored to GHL. Saving an
    # assessment must not create/update any GHL opportunity, so the
    # sync hooks are deliberately omitted here.
    def perform_create(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_assessment, serializer.instance, self.request)

    def perform_update(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_assessment, serializer.instance, self.request)

    def post_upsert(self, obj):
        _safe_timeline(timeline.event_for_assessment, obj, self.request)


class ProviderViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Provider.objects.all()
    serializer_class = ProviderSerializer


class ProgramViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Program.objects.all()
    serializer_class = ProgramSerializer

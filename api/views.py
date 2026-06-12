from rest_framework import generics, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.decorators import action
from rest_framework import status

from .models import (
    Case,
    Client,
    ContractedService,
    Eligibility,
    ImportBatch,
    Program,
    Provider,
    Screening,
)
from .serializers import (
    CaseSerializer,
    ClientSerializer,
    ContractedServiceSerializer,
    EligibilitySerializer,
    ImportBatchSerializer,
    ProgramSerializer,
    ProviderSerializer,
    RegisterSerializer,
    ScreeningSerializer,
    UserSerializer,
)
# TEMPORARY external-CRM mirror; remove with the api/integrations package.
from .integrations import ghl


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
        "addresses", "insurances", "military_profile"
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

    def perform_update(self, serializer):
        serializer.save(**self._agent_save_kwargs())
        ghl.sync_client(serializer.instance)

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

    def perform_update(self, serializer):
        serializer.save()
        ghl.sync_case(serializer.instance)

    def post_upsert(self, obj):
        ghl.sync_case(obj)


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

    def perform_update(self, serializer):
        serializer.save()
        ghl.sync_screening(serializer.instance)

    def post_upsert(self, obj):
        ghl.sync_screening(obj)


class EligibilityViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for eligibility assessments (keyed on eligibility_id UUID)."""

    queryset = Eligibility.objects.select_related("client")
    serializer_class = EligibilitySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # NOTE: Eligibility is intentionally NOT mirrored to GHL. Saving an
    # eligibility assessment must not create/update any GHL opportunity, so the
    # sync hooks are deliberately omitted here.


class ProviderViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Provider.objects.all()
    serializer_class = ProviderSerializer


class ProgramViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Program.objects.all()
    serializer_class = ProgramSerializer


class ImportBatchViewSet(viewsets.ModelViewSet):
    """Track import runs. Records the authenticated user as importer."""

    queryset = ImportBatch.objects.all()
    serializer_class = ImportBatchSerializer

    def perform_create(self, serializer):
        serializer.save(imported_by=self.request.user)

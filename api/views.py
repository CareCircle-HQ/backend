from rest_framework import generics, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.decorators import action
from rest_framework import status

from .models import (
    Case,
    Client,
    Eligibility,
    ImportBatch,
    Program,
    Provider,
    Screening,
)
from .serializers import (
    CaseSerializer,
    ClientSerializer,
    EligibilitySerializer,
    ImportBatchSerializer,
    ProgramSerializer,
    ProviderSerializer,
    RegisterSerializer,
    ScreeningSerializer,
    UserSerializer,
)


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


class ClientViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for clients (keyed on source client_id UUID)."""

    queryset = Client.objects.all().prefetch_related(
        "addresses", "insurances", "military_profile"
    )
    serializer_class = ClientSerializer


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


class ScreeningViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for screenings (keyed on enhanced_screen_id UUID)."""

    queryset = Screening.objects.select_related("client", "case", "template").prefetch_related(
        "answers", "identified_social_needs", "verified_social_needs"
    )
    serializer_class = ScreeningSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs


class EligibilityViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for eligibility assessments (keyed on eligibility_id UUID)."""

    queryset = Eligibility.objects.select_related("client", "case")
    serializer_class = EligibilitySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs


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

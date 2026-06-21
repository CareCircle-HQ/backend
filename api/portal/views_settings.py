"""Settings CRUD: menu types, dietary tags, kitchens, delivery companies and
their integrations."""

from django.shortcuts import get_object_or_404
from rest_framework import status as http, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import (
    DeliveryCompany,
    DeliveryCompanyIntegration,
    DietaryTag,
    Kitchen,
    KitchenIntegration,
    MenuType,
    MenuTypeTag,
)
from .base import PortalAPIView
from .permissions import IsPortalAgent
from . import serializers as s


def _clean_config(method, config, existing=None):
    """Persisted integration config. A masked apiKey ('********') means 'keep the
    existing key', so we never overwrite a real secret with the mask."""
    cfg = dict(config or {})
    if cfg.get("apiKey") == "********":
        cfg["apiKey"] = (existing or {}).get("apiKey", "")
    return cfg


class MenuTypeViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = MenuType.objects.all().prefetch_related("tags")
    serializer_class = s.PortalMenuTypeSerializer

    @action(detail=True, methods=["put"])
    def tags(self, request, pk=None):
        """Replace the menu type's dietary tags. Body: {"tag_ids": [...]}."""
        menu = self.get_object()
        tag_ids = request.data.get("tag_ids", [])
        MenuTypeTag.objects.filter(menu_type=menu).delete()
        for tid in tag_ids:
            tag = DietaryTag.objects.filter(pk=tid).first()
            if tag:
                MenuTypeTag.objects.get_or_create(menu_type=menu, dietary_tag=tag)
        menu.refresh_from_db()
        return Response(self.get_serializer(menu).data)


class DietaryTagViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = DietaryTag.objects.all()
    serializer_class = s.PortalDietaryTagSerializer

    def destroy(self, request, *args, **kwargs):
        tag = self.get_object()
        if tag.menu_type_tags.exists():
            return Response(
                {"error": "Remove this tag from all menu types before deleting it."},
                status=http.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class KitchenViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = Kitchen.objects.all().prefetch_related("menu_types", "integrations")
    serializer_class = s.PortalKitchenSerializer

    @action(detail=True, methods=["put"], url_path="menu-types")
    def menu_types(self, request, pk=None):
        kitchen = self.get_object()
        kitchen.menu_types.set(request.data.get("menu_type_ids", []))
        kitchen.refresh_from_db()
        return Response(self.get_serializer(kitchen).data)

    @action(detail=True, methods=["post"])
    def integrations(self, request, pk=None):
        """Add an integration. Kitchen integrations are unique per method, so a
        second integration of the same method is rejected (no is_primary)."""
        kitchen = self.get_object()
        method = request.data.get("method")
        if method not in ("email", "api"):
            return Response({"error": "method must be 'email' or 'api'."}, status=http.HTTP_400_BAD_REQUEST)
        if kitchen.integrations.filter(method=method).exists():
            return Response(
                {"error": f"This kitchen already has a {method} integration."},
                status=http.HTTP_409_CONFLICT,
            )
        integ = KitchenIntegration.objects.create(
            kitchen=kitchen, method=method, config=_clean_config(method, request.data.get("config")),
        )
        return Response(
            s.PortalKitchenIntegrationSerializer(integ).data, status=http.HTTP_201_CREATED
        )


class KitchenIntegrationDetailView(PortalAPIView):
    def patch(self, request, integration_id):
        integ = get_object_or_404(KitchenIntegration, pk=integration_id)
        if "config" in request.data:
            integ.config = _clean_config(integ.method, request.data["config"], integ.config)
        integ.save()
        return Response(s.PortalKitchenIntegrationSerializer(integ).data)

    def delete(self, request, integration_id):
        get_object_or_404(KitchenIntegration, pk=integration_id).delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class DeliveryCompanyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = DeliveryCompany.objects.all().prefetch_related("integrations")
    serializer_class = s.PortalDeliveryCompanySerializer

    @action(detail=True, methods=["post"])
    def integrations(self, request, pk=None):
        company = self.get_object()
        method = request.data.get("method")
        if method not in ("email", "api"):
            return Response({"error": "method must be 'email' or 'api'."}, status=http.HTTP_400_BAD_REQUEST)
        if company.integrations.filter(method=method).exists():
            return Response(
                {"error": f"This company already has a {method} integration."},
                status=http.HTTP_409_CONFLICT,
            )
        is_primary = bool(request.data.get("is_primary")) or not company.integrations.exists()
        if is_primary:
            company.integrations.update(is_primary=False)
        integ = DeliveryCompanyIntegration.objects.create(
            delivery_company=company,
            method=method,
            is_primary=is_primary,
            config=_clean_config(method, request.data.get("config")),
        )
        return Response(
            s.PortalDeliveryCompanyIntegrationSerializer(integ).data,
            status=http.HTTP_201_CREATED,
        )


class DeliveryCompanyIntegrationDetailView(PortalAPIView):
    def patch(self, request, integration_id):
        integ = get_object_or_404(DeliveryCompanyIntegration, pk=integration_id)
        if "config" in request.data:
            integ.config = _clean_config(integ.method, request.data["config"], integ.config)
        integ.save()
        return Response(s.PortalDeliveryCompanyIntegrationSerializer(integ).data)

    def delete(self, request, integration_id):
        get_object_or_404(DeliveryCompanyIntegration, pk=integration_id).delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class DeliveryCompanyIntegrationSetPrimaryView(PortalAPIView):
    def post(self, request, integration_id):
        integ = get_object_or_404(DeliveryCompanyIntegration, pk=integration_id)
        integ.delivery_company.integrations.update(is_primary=False)
        integ.is_primary = True
        integ.save(update_fields=["is_primary"])
        return Response(s.PortalDeliveryCompanyIntegrationSerializer(integ).data)

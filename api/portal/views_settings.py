"""Settings CRUD: menu types, dietary tags, kitchens, delivery companies and
their integrations."""

from django.shortcuts import get_object_or_404
from rest_framework import status as http, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import (
    Agent,
    Cadence,
    DeliveryCompany,
    DeliveryCompanyIntegration,
    DietaryTag,
    DietaryTagType,
    Kitchen,
    KitchenIntegration,
    KitchenMenuType,
    MenuType,
    MenuTypeTag,
    ProductType,
    ProgramMainCategory,
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


class CadenceViewSet(viewsets.ModelViewSet):
    """Settings > Delivery Cadences: manage the configurable delivery cadences
    (label + delivery weekdays). Configuration only for now -- surfaced in
    Settings and selectable per-kitchen; the scheduling core still reads the
    legacy enum/weekday map."""

    permission_classes = [IsPortalAgent]
    queryset = Cadence.objects.all()
    serializer_class = s.PortalCadenceSerializer
    pagination_class = None

    def destroy(self, request, *args, **kwargs):
        cadence = self.get_object()
        # Don't remove a cadence a ProductType is still built on -- deleting it
        # would orphan the option a live schedule depends on.
        if ProductType.objects.filter(delivery_days_cadence=cadence.code).exists():
            return Response(
                {"error": "A product type still uses this cadence. Reassign it before deleting."},
                status=http.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class KitchenViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = Kitchen.objects.all().prefetch_related(
        "menu_types",
        "cadences",
        "integrations",
        "kitchen_menu_types__menu_type",
        "kitchen_menu_types__restrictions",
    )
    serializer_class = s.PortalKitchenSerializer

    @action(detail=True, methods=["put"], url_path="menu-types")
    def menu_types(self, request, pk=None):
        kitchen = self.get_object()
        kitchen.menu_types.set(request.data.get("menu_type_ids", []))
        kitchen.refresh_from_db()
        return Response(self.get_serializer(kitchen).data)

    @action(detail=True, methods=["put"])
    def cadences(self, request, pk=None):
        """Set which delivery cadences this kitchen takes orders for.
        Body: {"cadence_ids": [...]}. Configuration only (not yet enforced)."""
        kitchen = self.get_object()
        kitchen.cadences.set(request.data.get("cadence_ids", []))
        kitchen.refresh_from_db()
        return Response(self.get_serializer(kitchen).data)

    @action(detail=True, methods=["put"], url_path="menu-type-config")
    def menu_type_config(self, request, pk=None):
        """Set the per-kitchen price and unmanageable allergies for ONE offered
        menu type. Body: {menu_type_id, price?, restriction_tag_ids?}.

        Only DietaryTags of type ``allergy`` are accepted as restrictions."""
        kitchen = self.get_object()
        mt_id = request.data.get("menu_type_id")
        kmt = (
            KitchenMenuType.objects.filter(kitchen=kitchen, menu_type_id=mt_id)
            .first()
        )
        if kmt is None:
            return Response(
                {"error": "This kitchen does not offer that menu type."},
                status=http.HTTP_404_NOT_FOUND,
            )
        if "price" in request.data:
            kmt.menu_type_price = request.data.get("price") or None
            kmt.save(update_fields=["menu_type_price"])
        if "restriction_tag_ids" in request.data:
            tags = DietaryTag.objects.filter(
                pk__in=request.data.get("restriction_tag_ids") or [],
                type=DietaryTagType.ALLERGY,
            )
            kmt.restrictions.set(tags)
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


class CrmAgentViewSet(viewsets.ModelViewSet):
    """Settings > CareCircle Agents: manage our internal CRM agent roster.

    Full list (no pagination, so the UI can search/filter client-side) plus
    create/update. Optional ``?search=`` (name/email/code) and ``?group=``
    query filters. Delete is disabled -- agents are deactivated via ``status``
    so historical references (tickets, cases) stay intact.
    """

    permission_classes = [IsPortalAgent]
    serializer_class = s.PortalCrmAgentSerializer
    pagination_class = None
    http_method_names = ["get", "post", "patch", "put", "head", "options"]

    def get_queryset(self):
        qs = Agent.objects.all().order_by("name")
        params = self.request.query_params
        group = (params.get("group") or "").strip()
        if group:
            qs = qs.filter(group=group)
        search = (params.get("search") or "").strip()
        if search:
            from django.db.models import Q

            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(agent_code__icontains=search)
                | Q(title__icontains=search)
            )
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        data = self.get_serializer(qs, many=True).data
        # Surface the selectable group choices so the UI dropdown stays in sync
        # with the model without hard-coding them on the frontend.
        return Response(
            {
                "count": len(data),
                "groups": [g[0] for g in Agent.AGENT_GROUPS],
                "results": data,
            }
        )


class ProgramMainCategoryViewSet(viewsets.ModelViewSet):
    """Settings > Program Categories: edit / activate / delete the program
    main-category master list.

    Categories are built up from Screening results, so there is NO create. They
    are opt-in: inactive by default, an admin activates the ones this org
    actually serves. Full list (no pagination for client-side search) with
    optional ``?search=`` (name) and ``?active=true|false``. Each row carries a
    read-only ``program_count`` (programs linked to the category).
    """

    permission_classes = [IsPortalAgent]
    serializer_class = s.PortalProgramMainCategorySerializer
    pagination_class = None
    http_method_names = ["get", "patch", "put", "delete", "head", "options"]

    def get_queryset(self):
        from django.db.models import Count

        qs = ProgramMainCategory.objects.annotate(program_count=Count("programs"))
        params = self.request.query_params
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        active = (params.get("active") or "").strip().lower()
        if active in ("true", "1"):
            qs = qs.filter(is_active=True)
        elif active in ("false", "0"):
            qs = qs.filter(is_active=False)
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        data = self.get_serializer(qs, many=True).data
        return Response(
            {
                "count": len(data),
                "active_count": sum(1 for c in data if c["is_active"]),
                "results": data,
            }
        )

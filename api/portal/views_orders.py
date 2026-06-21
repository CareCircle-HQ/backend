"""Global Orders page: purchase orders list/create, stats, lazy delivery
orders, and the send-to-kitchen / send-to-delivery actions."""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    DeliveryCompany,
    DeliveryOrder,
    Kitchen,
    PurchaseOrder,
    PurchaseOrderDeliveryStatus,
    PurchaseOrderKitchenStatus,
)
from .base import PortalAPIView, PortalGenericAPIView
from . import serializers as s

PO_PREFETCH = ("delivery_orders", "kitchen", "delivery_company")


class PurchaseOrdersView(PortalGenericAPIView):
    serializer_class = s.PortalPurchaseOrderSerializer

    def get_queryset(self):
        qs = PurchaseOrder.objects.all().prefetch_related(*PO_PREFETCH)
        p = self.request.query_params
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(kitchen__name__icontains=search)
                | Q(delivery_company__name__icontains=search)
                | Q(delivery_orders__member__first_name__icontains=search)
                | Q(delivery_orders__member__last_name__icontains=search)
            ).distinct()
        status_val = (p.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            qs = qs.filter(status=status_val.lower())
        kitchen = (p.get("kitchen") or "").strip()
        if kitchen and kitchen.lower() != "all":
            qs = qs.filter(kitchen_id=kitchen)
        if p.get("from"):
            qs = qs.filter(delivery_date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(delivery_date__lte=p["to"])
        return qs

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def post(self, request):
        # Create a DRAFT purchase order (no notes field on the model).
        delivery_date = request.data.get("delivery_date") or None
        po = PurchaseOrder.objects.create(
            delivery_date=delivery_date,
            kitchen_id=request.data.get("kitchen_id") or None,
            delivery_company_id=request.data.get("delivery_company_id") or None,
            status="draft",
        )
        po = PurchaseOrder.objects.prefetch_related(*PO_PREFETCH).get(pk=po.pk)
        return Response(
            s.PortalPurchaseOrderSerializer(po).data, status=http.HTTP_201_CREATED
        )


class PurchaseOrdersStatsView(PortalAPIView):
    def get(self, request):
        qs = PurchaseOrder.objects.all()
        return Response(
            {
                "total": qs.count(),
                "confirmed": qs.filter(status="confirmed").count(),
                "completed": qs.filter(status="completed").count(),
                "cancelled": qs.filter(status="cancelled").count(),
            }
        )


class PurchaseOrderDeliveryOrdersView(PortalGenericAPIView):
    """Lazy-loaded delivery orders for one PO (loaded when a PO is expanded)."""

    serializer_class = s.PortalDeliveryOrderSerializer

    def get(self, request, po_id):
        get_object_or_404(PurchaseOrder, pk=po_id)
        qs = (
            DeliveryOrder.objects.filter(purchase_order_id=po_id)
            .select_related("member", "group", "kitchen", "menu_type", "delivery_company")
            .prefetch_related("custom_dietary_tags")
        )
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


class SendToKitchenView(PortalAPIView):
    def post(self, request, po_id):
        po = get_object_or_404(PurchaseOrder, pk=po_id)
        po.kitchen_status = PurchaseOrderKitchenStatus.SENT_TO_KITCHEN
        po.sent_to_kitchen_at = timezone.now()
        if po.status == "draft":
            po.status = "confirmed"
        po.save(update_fields=["kitchen_status", "sent_to_kitchen_at", "status", "updated_at"])
        po = PurchaseOrder.objects.prefetch_related(*PO_PREFETCH).get(pk=po.pk)
        return Response(s.PortalPurchaseOrderSerializer(po).data)


class SendToDeliveryView(PortalAPIView):
    def post(self, request, po_id):
        po = get_object_or_404(PurchaseOrder, pk=po_id)
        po.delivery_status = PurchaseOrderDeliveryStatus.SENT_TO_DELIVERY
        po.sent_to_delivery_at = timezone.now()
        po.save(update_fields=["delivery_status", "sent_to_delivery_at", "updated_at"])
        po = PurchaseOrder.objects.prefetch_related(*PO_PREFETCH).get(pk=po.pk)
        return Response(s.PortalPurchaseOrderSerializer(po).data)


class KitchensListView(PortalAPIView):
    """Lightweight list for Orders filters/dropdowns."""

    def get(self, request):
        kitchens = Kitchen.objects.all().order_by("name")
        return Response(
            [{"id": str(k.pk), "name": k.name, "status": k.status} for k in kitchens]
        )


class DeliveryCompaniesListView(PortalAPIView):
    def get(self, request):
        companies = DeliveryCompany.objects.all().order_by("name")
        return Response(
            [{"id": str(c.pk), "name": c.name, "status": c.status} for c in companies]
        )

"""Global Orders page: purchase orders list/create, stats, lazy delivery
orders, and the send-to-kitchen / send-to-delivery actions."""

import uuid

from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response

from datetime import date as _date

from ..models import (
    DeliveryCompany,
    DeliveryOrder,
    DeliveryOrderStatus,
    Kitchen,
    ProductTypeKind,
    PurchaseOrder,
    PurchaseOrderDeliveryStatus,
    PurchaseOrderKitchenStatus,
    PurchaseOrderStatus,
)
from ..services.orders import resync_scheduled_orders
from ..services.purchase_orders import (
    backfill_late_occurrences,
    build_kitchen_export_csv,
    build_po_summary_data,
    build_po_summary_report,
    generate_purchase_order,
    preview_purchase_orders,
    split_purchase_order,
)
from .base import PortalAPIView, PortalGenericAPIView
from . import serializers as s


def _parse_date(value):
    """Parse an ISO date string (YYYY-MM-DD); return None on failure."""
    if not value:
        return None
    try:
        return _date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _parse_uuid(value):
    """Parse ``value`` as a UUID; return None if it isn't one."""
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None

PO_PREFETCH = ("delivery_orders", "kitchen", "delivery_company", "notes")


class PurchaseOrdersView(PortalGenericAPIView):
    serializer_class = s.PortalPurchaseOrderSerializer

    def get_queryset(self):
        qs = PurchaseOrder.objects.all().prefetch_related(*PO_PREFETCH)
        p = self.request.query_params
        search = (p.get("search") or "").strip()
        if search:
            cond = (
                Q(po_number__icontains=search)
                | Q(kitchen__name__icontains=search)
                | Q(delivery_company__name__icontains=search)
                | Q(delivery_orders__member__first_name__icontains=search)
                | Q(delivery_orders__member__last_name__icontains=search)
            )
            # UUID columns need an exact match (icontains isn't valid on a
            # UUIDField). Match the PO id, the delivery-order ("order") id, and
            # the member id so agents can paste any of those.
            uid = _parse_uuid(search)
            if uid:
                cond |= (
                    Q(pk=uid)
                    | Q(delivery_orders__delivery_order_id=uid)
                    | Q(delivery_orders__member__client_id=uid)
                )
            qs = qs.filter(cond).distinct()
        status_val = (p.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            qs = qs.filter(status=status_val.lower())
        else:
            # Cancelled POs are hidden from the default "All" view; they only
            # appear when the Cancelled filter is explicitly selected.
            qs = qs.exclude(status=PurchaseOrderStatus.CANCELLED)
        kind = (p.get("kind") or "").strip()
        if kind and kind.lower() != "all":
            qs = qs.filter(kind=kind.lower())
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
            .prefetch_related("custom_dietary_tags", "proofs")
        )
        # Search within the PO: match the delivery order's member by Client ID
        # or name, AND the delivery order itself by its ORDER # (delivery order
        # id) -- so an agent can paste an ORDER # from a delivery report to
        # pinpoint that exact order (and its proof) inside the PO. Searches the
        # whole PO, not just the current page.
        search = (request.query_params.get("search") or "").strip()
        if search:
            cond = (
                Q(member__first_name__icontains=search)
                | Q(member__last_name__icontains=search)
            )
            parts = search.split()
            if len(parts) >= 2:
                cond |= Q(member__first_name__icontains=parts[0]) & Q(
                    member__last_name__icontains=parts[-1]
                )
            uid = _parse_uuid(search)
            if uid:
                # Full UUID -> exact match on the order id or the member id.
                cond |= Q(delivery_order_id=uid) | Q(member__client_id=uid)
            else:
                # Partial -> match against the textual form of either id
                # (UUIDField can't be icontains-searched directly, so cast).
                qs = qs.annotate(
                    _member_cid=Cast("member__client_id", output_field=TextField()),
                    _do_id=Cast("delivery_order_id", output_field=TextField()),
                )
                cond |= Q(_member_cid__icontains=search) | Q(_do_id__icontains=search)
            qs = qs.filter(cond)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


class PurchaseOrderNotesView(PortalAPIView):
    """GET the note history for a PO (newest first) + POST to add a note. Each
    note records the author agent + a timestamp."""

    def get(self, request, po_id):
        get_object_or_404(PurchaseOrder, pk=po_id)
        from ..models import PurchaseOrderNote

        notes = PurchaseOrderNote.objects.filter(
            purchase_order_id=po_id
        ).order_by("-created_at")
        return Response(s.PortalPurchaseOrderNoteSerializer(notes, many=True).data)

    def post(self, request, po_id):
        from .base import current_agent
        from ..models import PurchaseOrderNote

        po = get_object_or_404(PurchaseOrder, pk=po_id)
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response(
                {"error": "Note body is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        agent = current_agent(request)
        note = PurchaseOrderNote.objects.create(
            purchase_order=po,
            author_agent=agent,
            author_name=agent.name if agent else "",
            body=body,
        )
        return Response(
            s.PortalPurchaseOrderNoteSerializer(note).data, status=http.HTTP_201_CREATED
        )


class SendToKitchenView(PortalAPIView):
    def post(self, request, po_id):
        po = get_object_or_404(PurchaseOrder, pk=po_id)
        po.kitchen_status = PurchaseOrderKitchenStatus.SENT_TO_KITCHEN
        po.sent_to_kitchen_at = timezone.now()
        if po.status == "draft":
            po.status = "confirmed"
        po.save(update_fields=["kitchen_status", "sent_to_kitchen_at", "status", "updated_at"])
        # Advance each line item so the kitchen-prepared orders are flagged
        # ready to hand off to the delivery company. Only move items still in
        # the initial PENDING state to avoid overriding later lifecycle states.
        po.delivery_orders.filter(status=DeliveryOrderStatus.PENDING).update(
            status=DeliveryOrderStatus.READY_FOR_DELIVERY,
            updated_at=timezone.now(),
        )
        po = PurchaseOrder.objects.prefetch_related(*PO_PREFETCH).get(pk=po.pk)
        return Response(s.PortalPurchaseOrderSerializer(po).data)


class KitchenExportView(PortalAPIView):
    """Download the kitchen export CSV for a PO (the file handed to the kitchen
    when the PO is dispatched). Filename embeds the PO number + kitchen."""

    def get(self, request, po_id):
        po = get_object_or_404(
            PurchaseOrder.objects.select_related("kitchen"), pk=po_id
        )
        filename, csv_text = build_kitchen_export_csv(po)
        resp = HttpResponse(csv_text, content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp["X-Export-Filename"] = filename
        return resp


class PurchaseOrderReportView(PortalAPIView):
    """Download a per-order summary CSV for a PO: totals (members + total
    meals/boxes) and a per-menu-type breakdown."""

    def get(self, request, po_id):
        po = get_object_or_404(
            PurchaseOrder.objects.select_related("kitchen"), pk=po_id
        )
        filename, csv_text = build_po_summary_report(po)
        resp = HttpResponse(csv_text, content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp["X-Export-Filename"] = filename
        return resp


class PurchaseOrderReportDataView(PortalAPIView):
    """Structured (JSON) per-order summary for the in-app report view: totals,
    per-menu-type breakdown, and per-household member lines."""

    def get(self, request, po_id):
        po = get_object_or_404(
            PurchaseOrder.objects.select_related("kitchen"), pk=po_id
        )
        return Response(build_po_summary_data(po))


class CancelPurchaseOrderView(PortalAPIView):
    """Cancel a purchase order: mark the PO cancelled and cancel its still-open
    delivery orders. Line items that already reached a terminal state
    (delivered/returned/failed/cancelled) are left untouched. A completed or
    already-cancelled PO can't be cancelled again."""

    def post(self, request, po_id):
        po = get_object_or_404(PurchaseOrder, pk=po_id)
        if po.status in (
            PurchaseOrderStatus.CANCELLED,
            PurchaseOrderStatus.COMPLETED,
        ):
            return Response(
                {
                    "detail": (
                        f"A {po.get_status_display().lower()} purchase order "
                        "can't be cancelled."
                    )
                },
                status=http.HTTP_409_CONFLICT,
            )
        po.status = PurchaseOrderStatus.CANCELLED
        po.save(update_fields=["status", "updated_at"])
        po.delivery_orders.exclude(
            status__in=(
                DeliveryOrderStatus.DELIVERED,
                DeliveryOrderStatus.CANCELLED,
                DeliveryOrderStatus.RETURNED,
                DeliveryOrderStatus.FAILED,
            )
        ).update(status=DeliveryOrderStatus.CANCELLED, updated_at=timezone.now())
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


def _parse_kind(value):
    value = (value or "").strip().lower()
    if value in (ProductTypeKind.MEALS, ProductTypeKind.BOXES):
        return value
    return None


class PurchaseOrderPreviewView(PortalAPIView):
    """Aggregate the delivery calendar for a (kind, delivery_date) into a
    per-kitchen / per-menu-type breakdown with capacity, ready to turn into POs."""

    def get(self, request):
        kind = _parse_kind(request.query_params.get("kind"))
        delivery_date = _parse_date(request.query_params.get("delivery_date"))
        if kind is None or delivery_date is None:
            return Response(
                {"detail": "kind (meals|boxes) and delivery_date (YYYY-MM-DD) are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        return Response(preview_purchase_orders(kind, delivery_date))


class PurchaseOrderPreviewRefreshView(PortalAPIView):
    """Re-sync the future delivery occurrences for a date from live member data,
    then return the fresh preview.

    The PO popup's per-kitchen breakdown reads point-in-time snapshots on the
    delivery calendar (kitchen + menu type). This endpoint backs the "Refresh"
    button: it pulls the CURRENT menu type / allergies / assigned kitchen onto
    the still-SCHEDULED occurrences on ``delivery_date`` so a reassigned member
    lands under the right kitchen and a corrected menu stops showing as
    "unsupported"."""

    def post(self, request):
        kind = _parse_kind(request.data.get("kind"))
        delivery_date = _parse_date(request.data.get("delivery_date"))
        if kind is None or delivery_date is None:
            return Response(
                {"detail": "kind (meals|boxes) and delivery_date (YYYY-MM-DD) are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Only re-pull live kitchen / menu / allergy snapshots onto the SCHEDULED
        # occurrences ON THIS DATE (what the preview reads). The full-calendar
        # reconcile (sync_active_calendars) walked every active enrollment's
        # entire future window and timed out the request (504), which surfaces in
        # the browser as a missing-CORS error.
        updated = resync_scheduled_orders(delivery_date=delivery_date)
        data = preview_purchase_orders(kind, delivery_date)
        data["refreshed"] = {"updated": updated}
        return Response(data)


class PurchaseOrderPreviewLateView(PortalAPIView):
    """Backfill occurrences for a date whose PO cutoff has already passed, then
    return the preview.

    When a cadence change lands after a delivery date's cutoff, that date is
    skipped by the calendar (the plan's first delivery moves to the next
    orderable date), so the preview is empty and no LATE PO can be cut. This
    endpoint synthesizes SCHEDULED occurrences for members whose cadence delivers
    that weekday -- skipping anyone already covered by a live delivery that week
    (no double-delivery) -- so the agent can still batch a late PO."""

    def post(self, request):
        kind = _parse_kind(request.data.get("kind"))
        delivery_date = _parse_date(request.data.get("delivery_date"))
        if kind is None or delivery_date is None:
            return Response(
                {"detail": "kind (meals|boxes) and delivery_date (YYYY-MM-DD) are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        added = backfill_late_occurrences(kind, delivery_date)
        data = preview_purchase_orders(kind, delivery_date)
        data["late_backfill"] = {"added": added}
        return Response(data)


class PrepareMembersForPOView(PortalAPIView):
    """Start / poll the async "Prepare Members for PO" job.

    A full-calendar reconcile across every active household is far too slow for a
    web request (it 504s inline), so POST enqueues a Celery worker that refreshes
    the whole delivery calendar -- adding newly-eligible members, dropping those
    no longer part of a servable household, and freeing dates whose PO was
    cancelled -- and writes live progress to a tracking ``ImportRun``. GET polls
    that row for the progress bar + completion (mirrors the CSV import flow)."""

    def _latest(self):
        from ..models import ImportRun
        from ..tasks import MEMBER_PREP_SOURCE

        return (
            ImportRun.objects.filter(source=MEMBER_PREP_SOURCE)
            .order_by("-started_at")
            .first()
        )

    def get(self, request):
        from .views_imports import _run_summary

        run = self._latest()
        if run is None:
            return Response({"status": "idle", "run": None})
        return Response(_run_summary(run))

    def post(self, request):
        from ..models import ImportRun, ImportRunStatus
        from ..tasks import MEMBER_PREP_SOURCE, prepare_members_for_po
        from .base import current_agent
        from .views_imports import _run_summary

        # Idempotent: if a prep job is already in flight, return it instead of
        # spawning a second heavy full-calendar pass.
        existing = self._latest()
        if existing is not None and existing.status in (
            ImportRunStatus.PENDING, ImportRunStatus.RUNNING,
        ):
            return Response(_run_summary(existing), status=http.HTTP_202_ACCEPTED)

        agent = current_agent(request)
        triggered_by = (
            f"agent:{agent.agent_code}" if agent and agent.agent_code else "manual"
        )
        run = ImportRun.objects.create(
            source=MEMBER_PREP_SOURCE,
            status=ImportRunStatus.PENDING,
            triggered_by=triggered_by,
        )
        prepare_members_for_po.delay(run.pk)
        return Response(_run_summary(run), status=http.HTTP_202_ACCEPTED)


class PurchaseOrderGenerateView(PortalAPIView):
    """Create one PO for a kitchen on a delivery date from selected member
    schedules (the agent's % / subset selection)."""

    def post(self, request):
        kind = _parse_kind(request.data.get("kind"))
        delivery_date = _parse_date(request.data.get("delivery_date"))
        kitchen_id = request.data.get("kitchen_id") or None
        schedule_ids = request.data.get("schedule_ids") or []
        if kind is None or delivery_date is None or not schedule_ids:
            return Response(
                {"detail": "kind, delivery_date and schedule_ids are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        kitchen = Kitchen.objects.filter(pk=kitchen_id).first() if kitchen_id else None
        po = generate_purchase_order(kind, delivery_date, kitchen, schedule_ids)
        if po is None:
            return Response(
                {"detail": "No eligible schedules to batch (already ordered?)."},
                status=http.HTTP_409_CONFLICT,
            )
        po = PurchaseOrder.objects.prefetch_related(*PO_PREFETCH).get(pk=po.pk)
        return Response(
            s.PortalPurchaseOrderSerializer(po).data, status=http.HTTP_201_CREATED
        )


class PurchaseOrderSplitView(PortalAPIView):
    """Move selected DeliveryOrders out of a PO into a new PO on another date."""

    def post(self, request, po_id):
        po = get_object_or_404(PurchaseOrder, pk=po_id)
        new_date = _parse_date(request.data.get("delivery_date"))
        delivery_order_ids = request.data.get("delivery_order_ids") or []
        if new_date is None or not delivery_order_ids:
            return Response(
                {"detail": "delivery_date and delivery_order_ids are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        new_po = split_purchase_order(po, delivery_order_ids, new_date)
        if new_po is None:
            return Response(
                {"detail": "No matching delivery orders to move."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        new_po = PurchaseOrder.objects.prefetch_related(*PO_PREFETCH).get(pk=new_po.pk)
        return Response(
            s.PortalPurchaseOrderSerializer(new_po).data, status=http.HTTP_201_CREATED
        )

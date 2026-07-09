"""Portal API: PO Blockers.

Read-only surface over :func:`api.services.po_blockers.classify_po_blockers` --
the members who have a live delivery plan but won't reach a Purchase Order,
bucketed by cause. Backs the Logistics > PO Blockers page.
"""
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from api.models import EnrollmentVerification
from api.services.po_blockers import (
    BLOCKED_REASONS,
    FIXABLE_REASONS,
    REASON_DESCRIPTIONS,
    REASON_LABELS,
    REASON_ORDER,
    classify_po_blockers,
    remediate_enrollment_blocker,
    summarize_po_blockers,
)

from .base import PortalAPIView
from .pagination import PortalPagination


def _order_index(reason):
    return REASON_ORDER.index(reason) if reason in REASON_ORDER else len(REASON_ORDER)


class POBlockersView(PortalAPIView):
    """GET /api/portal/po-blockers/ -- paginated list of blocked members.

    Query params:
      * ``reason``  -- filter to a single reason code.
      * ``search``  -- match member name, client id, or program name.
      * ``page`` / ``page_size`` -- standard portal pagination.
    """

    def get(self, request):
        rows = classify_po_blockers(include_ok=False)

        reason = (request.query_params.get("reason") or "").strip()
        if reason and reason != "all":
            rows = [r for r in rows if r["reason"] == reason]

        search = (request.query_params.get("search") or "").strip().lower()
        if search:
            rows = [
                r for r in rows
                if search in r["member_name"].lower()
                or search in r["client_id"].lower()
                or search in r["program_name"].lower()
            ]

        rows.sort(key=lambda r: (_order_index(r["reason"]), r["member_name"].lower()))

        paginator = PortalPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(page)


class POBlockersStatsView(PortalAPIView):
    """GET /api/portal/po-blockers/stats/ -- per-reason counts + reason metadata.

    Returns the full breakdown (unfiltered) so the page can render the summary
    cards and the reason filter regardless of the current filter.
    """

    def get(self, request):
        rows = classify_po_blockers(include_ok=False)
        counts = summarize_po_blockers(rows)
        reasons = [
            {
                "reason": r,
                "label": REASON_LABELS.get(r, r),
                "description": REASON_DESCRIPTIONS.get(r, ""),
                "count": counts.get(r, 0),
                "fixable": r in FIXABLE_REASONS,
            }
            for r in BLOCKED_REASONS
        ]
        return Response({
            "total": len(rows),
            "reasons": reasons,
        })


class POBlockersFixView(PortalAPIView):
    """POST /api/portal/po-blockers/fix/ -- apply the one-click fix for a member.

    Body: ``{enrollment_id, reason}``. Fixable reasons (lapsed window / calendar
    not generated / cadence-weekday mismatch / program switched / stale case
    link) are remediated server-side (recompute the delivery plan + rebuild the
    calendar, or repoint the case). Non-fixable reasons -- including the
    informational ``program_switch_pending`` / ``duplicate_open_cases`` -- return
    ``fixed=False`` with guidance.
    """

    @transaction.atomic
    def post(self, request):
        enrollment_id = request.data.get("enrollment_id")
        reason = (request.data.get("reason") or "").strip()
        if not enrollment_id or not reason:
            return Response(
                {"error": "enrollment_id and reason are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        enr = get_object_or_404(EnrollmentVerification, pk=enrollment_id)
        result = remediate_enrollment_blocker(enr, reason)
        status_code = http.HTTP_200_OK if result.get("fixed") else http.HTTP_422_UNPROCESSABLE_ENTITY
        return Response(result, status=status_code)

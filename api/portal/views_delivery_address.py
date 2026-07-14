"""Customer Service -> Delivery Address: active-service households whose delivery
address has a DETECTED FORMAT PROBLEM, in one editable table.

The delivery address is HOUSEHOLD-WIDE (one :class:`~api.models.Address` per
enrollment, shared by all members), so this returns ONE ROW PER HOUSEHOLD. Only
households whose address fails a format check (unit embedded in the street line,
duplicate unit, PO box, missing street/city/state) are returned -- clean
addresses are omitted. Detection lives in ``api.services.address_quality``.

Edits are saved through the existing ``MemberHouseholdView`` PATCH
(``/members/<client_id>/household/``) so the delivery-coverage check + timeline
logging stay in one place -- this module is read-only.
"""

from django.db.models import Count, Q
from rest_framework.response import Response

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.address_quality import detect_address_issues, issue_label
from .base import PortalAPIView, current_agent

# Delivery Address is a CS tool: CS + Management (and manager override).
_ALLOWED_GROUPS = ("CS", "Management")

PAGE_SIZE = 25


def _can_access(agent):
    if not agent:
        return False
    return agent.group in _ALLOWED_GROUPS or getattr(agent, "is_manager", False)


def _client_name(client):
    if client is None:
        return "Unknown"
    name = f"{(client.first_name or '').strip()} {(client.last_name or '').strip()}".strip()
    return name or "Unknown"


def _address_string(addr):
    if addr is None:
        return ""
    parts = [addr.street, addr.unit, addr.city, addr.state, addr.zip]
    return ", ".join(p for p in (x.strip() for x in parts if x) if p)


class DeliveryAddressListView(PortalAPIView):
    """GET /portal/delivery-addresses/ — active-service households whose delivery
    address has a detected format problem, for the CS Delivery Address table.

    Query params:
      * ``search`` = member name, client id, or any part of the address
      * ``issue``  = a specific issue code (e.g. ``unit_in_street``)
      * ``page``
    Returns a summary (counts by issue code across the full match) plus a page
    of household rows, each carrying the issues it triggered.
    """

    def get(self, request):
        agent = current_agent(request)
        if not _can_access(agent):
            return Response(
                {"detail": "Delivery Address access required."}, status=403
            )

        params = request.query_params
        search = (params.get("search") or "").strip()
        issue = (params.get("issue") or "").strip()
        try:
            page = max(1, int(params.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        qs = (
            EnrollmentVerification.objects
            .filter(stage=EnrollmentStage.SERVICE_ACTIVE, delivery_address__isnull=False)
            .select_related("client", "delivery_address", "kitchen")
            .annotate(member_count=Count("member_profiles", distinct=True))
        )
        if search:
            qs = qs.filter(
                Q(client__first_name__icontains=search)
                | Q(client__last_name__icontains=search)
                | Q(client__client_id__icontains=search)
                | Q(delivery_address__street__icontains=search)
                | Q(delivery_address__city__icontains=search)
                | Q(delivery_address__zip__icontains=search)
            )
        qs = qs.order_by("client__last_name", "client__first_name", "pk")

        # Detection is Python-side (regex heuristics), so scan the matched set,
        # keep only households WITH an issue, and tally counts by code. The scan
        # is cheap (a few thousand short strings).
        by_code = {}
        flagged_households = 0
        matched = []
        for enr in qs.iterator():
            addr = enr.delivery_address
            codes = detect_address_issues(
                addr.street, addr.unit, addr.city, addr.state, addr.zip
            )
            if not codes:
                continue
            flagged_households += 1
            for c in codes:
                by_code[c] = by_code.get(c, 0) + 1
            if issue and issue not in codes:
                continue
            matched.append((enr, addr, codes))

        total = len(matched)
        start = (page - 1) * PAGE_SIZE
        page_rows = matched[start:start + PAGE_SIZE]

        results = []
        for enr, addr, codes in page_rows:
            results.append({
                "enrollment_id": enr.pk,
                "client_id": str(enr.client_id) if enr.client_id else None,
                "household_name": _client_name(enr.client),
                "member_count": enr.member_count,
                "stage": enr.stage,
                "kitchen_name": enr.kitchen.name if enr.kitchen_id else None,
                "address": {
                    "street": addr.street,
                    "unit": addr.unit,
                    "city": addr.city,
                    "state": addr.state,
                    "zip": addr.zip,
                    "notes": addr.notes,
                },
                "address_string": _address_string(addr),
                "issues": [{"code": c, "label": issue_label(c)} for c in codes],
            })

        return Response({
            "summary": {
                "flagged": flagged_households,
                "by_code": by_code,
            },
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "has_more": start + len(page_rows) < total,
            "results": results,
        })

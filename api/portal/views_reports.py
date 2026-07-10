"""Admin > Reports: management-only CSV exports.

Each report is a management-gated endpoint that streams a downloadable CSV. The
first report exports members carrying a given lead source (e.g. the "Hyphen Met"
CallTools queue) within an optional Client.created_at date range.
"""

import csv
import functools
import operator

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.response import Response

from ..models import Case, CaseType, Client
from .base import PortalAPIView, current_agent
from .serializers import medicaid_member_id
from .views_members import _parse_date


def _lead_source_label_map():
    """Best-effort CallTools queue id -> name map, so a stored lead_source that is
    a queue id (e.g. "5851") resolves to its friendly label ("Hyphen Met") in the
    export. Never lets a CallTools hiccup break the report."""
    mapping = {}
    try:
        from ..integrations.calltools import campaigns as ct_campaigns
        from ..integrations.calltools import config as ct_config
        from ..integrations.calltools import queues as ct_queues

        if ct_config.is_enabled():
            ct_options = []
            try:
                ct_options += ct_queues.list_queue_options()
            except Exception:
                pass
            try:
                ct_options += ct_campaigns.list_campaign_options()
            except Exception:
                pass
            for q in ct_options:
                qid = str(q.get("id") or "").strip()
                name = (q.get("name") or "").strip()
                if qid and name:
                    mapping.setdefault(qid, name)
    except Exception:
        pass
    return mapping


class MembersByLeadSourceReportView(PortalAPIView):
    """Management-only CSV export of members carrying a given lead source.

    Query params:
        lead_source   -- required; one OR MORE lead sources (repeat the param, or
                         pass a comma-separated list). Each is matched
                         case-insensitively against Client.lead_source; a member
                         is included if it matches ANY selected source. Values
                         come from the same dropdown the Members page uses.
        created_from  -- optional inclusive lower bound on Client.created_at (date).
        created_to    -- optional inclusive upper bound on Client.created_at (date).
    """

    def get(self, request):
        agent = current_agent(request)
        if not (agent and (agent.group == "Management" or getattr(agent, "is_manager", False))):
            return Response({"detail": "Management access required."}, status=403)

        # Accept repeated ?lead_source= params AND a comma-separated fallback.
        lead_sources = [v.strip() for v in request.query_params.getlist("lead_source") if v.strip()]
        if not lead_sources:
            lead_sources = [
                v.strip()
                for v in (request.query_params.get("lead_source") or "").split(",")
                if v.strip()
            ]
        if not lead_sources:
            return Response({"detail": "At least one lead_source is required."}, status=400)

        created_from = _parse_date(request.query_params.get("created_from"))
        created_to = _parse_date(request.query_params.get("created_to"))

        # Match ANY of the selected sources (case-insensitive).
        source_filter = functools.reduce(
            operator.or_, (Q(lead_source__iexact=v) for v in lead_sources)
        )
        qs = (
            Client.objects.filter(source_filter)
            .prefetch_related(
                "insurances",
                "screenings",
                "cases",
                "household_membership__household__members",
            )
            .order_by("created_at")
        )
        if created_from:
            qs = qs.filter(created_at__date__gte=created_from)
        if created_to:
            qs = qs.filter(created_at__date__lte=created_to)

        label_map = _lead_source_label_map()

        response = HttpResponse(content_type="text/csv")
        filename = f"members_by_lead_source_{timezone.localdate().isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Member ID",
            "Phone Number",
            "Medicaid ID",
            "Is there Screening",
            "Is there Eligibility",
            "Is there Internal Service Case",
            "Household Member Count (if multi-member)",
            "Lead Source",
        ])

        for client in qs:
            cases = list(client.cases.all())
            has_screening = len(list(client.screenings.all())) > 0
            has_eligibility = any(c.case_type == CaseType.ELIGIBILITY for c in cases)
            has_internal_service = any(
                c.case_type == CaseType.INTERNAL_SERVICE for c in cases
            )

            # Household member count only when the client belongs to a
            # multi-member household; blank otherwise (per the report spec).
            household_count = ""
            membership = getattr(client, "household_membership", None)
            if membership is not None:
                member_total = len(list(membership.household.members.all()))
                if member_total > 1:
                    household_count = member_total

            raw_source = (client.lead_source or "").strip()
            source_label = label_map.get(raw_source, raw_source)

            writer.writerow([
                str(client.client_id),
                client.client_phone_number or "",
                medicaid_member_id(client),
                "Yes" if has_screening else "No",
                "Yes" if has_eligibility else "No",
                "Yes" if has_internal_service else "No",
                household_count,
                source_label,
            ])

        return response

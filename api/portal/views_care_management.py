"""Customer Service -> Care Management: households with active warnings.

Reads the persisted :class:`~api.models.MemberWarning` snapshot (kept current by
the on-open live scan, the case-save / import hooks and the nightly sweep) and
returns ONE ROW PER HOUSEHOLD so CS can work the queue of members with problems.

Detection lives in ``api.services.warnings``; this endpoint only queries the
snapshot, so it is cheap and never recomputes across the whole DB.
"""

from django.db.models import Q
from rest_framework.response import Response

from api.models import (
    MemberWarning,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    WarningSeverity,
    WarningStatus,
)
from api.services.warnings import CARE_MANAGEMENT_CODES
from .base import PortalAPIView, current_agent

# Care Management is a CS queue: CS + Management (and manager override).
_ALLOWED_GROUPS = ("CS", "Management")

_SEVERITY_RANK = {WarningSeverity.RED: 2, WarningSeverity.ORANGE: 1}

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


class CareManagementListView(PortalAPIView):
    """GET /portal/care-management/ — households with active warnings.

    Query params:
      * ``severity`` = red | orange
      * ``code``     = a specific warning code
      * ``kitchen``  = kitchen id (or "unassigned")
      * ``search``   = member name or client id (matches any household member)
      * ``page``
    Returns summary counts across the full match plus a page of household rows.
    """

    def get(self, request):
        agent = current_agent(request)
        if not _can_access(agent):
            return Response(
                {"detail": "Care Management access required."}, status=403
            )

        params = request.query_params
        severity = (params.get("severity") or "").lower()
        code = (params.get("code") or "").strip()
        kitchen = (params.get("kitchen") or "").strip()
        search = (params.get("search") or "").strip()
        try:
            page = max(1, int(params.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        # Only surface warnings CS can actually REMEDIATE here. Informational
        # member/household states (out of orbit/range, paused, on hold,
        # cancelled) are excluded via the CARE_MANAGEMENT_CODES allowlist so a
        # household is never flagged onto this queue for a problem that can't be
        # fixed on this page. Filtering at the row level (before grouping) also
        # keeps those informational rows from cluttering a household surfaced for
        # a real issue.
        #
        # Also never surface households that aren't being served: On Hold (case
        # under review), Cancelled, Closed or Service Complete. Filtered here (not
        # just at detection) so a stale snapshot can never leak them into the
        # queue. Rows with no enrollment link (fallback client grouping) have a
        # NULL stage and are kept.
        rows = (
            MemberWarning.objects.filter(
                status=WarningStatus.ACTIVE, code__in=CARE_MANAGEMENT_CODES
            )
            .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
            .select_related(
                "client", "enrollment", "enrollment__client", "enrollment__kitchen"
            )
        )
        if severity in (WarningSeverity.RED, WarningSeverity.ORANGE):
            rows = rows.filter(severity=severity)
        if code:
            rows = rows.filter(code=code)
        if kitchen == "unassigned":
            rows = rows.filter(enrollment__kitchen__isnull=True)
        elif kitchen:
            rows = rows.filter(enrollment__kitchen_id=kitchen)

        rows = list(rows)

        # Group by household (enrollment). Rows without an enrollment link fall
        # back to grouping on the attached client so nothing is dropped.
        households = {}
        for r in rows:
            key = r.enrollment_id or f"client:{r.client_id}"
            hh = households.get(key)
            if hh is None:
                enr = r.enrollment
                primary = enr.client if enr is not None else r.client
                kitchen_obj = enr.kitchen if enr is not None else None
                hh = households[key] = {
                    "enrollment_id": enr.pk if enr is not None else None,
                    "client_id": str(primary.pk) if primary is not None else str(r.client_id),
                    "household_name": _client_name(primary),
                    "stage": enr.stage if enr is not None else None,
                    "kitchen_name": kitchen_obj.name if kitchen_obj is not None else None,
                    "warnings": [],
                    "_members": {},
                    "_max_rank": 0,
                }
            member_name = _client_name(r.client)
            hh["warnings"].append({
                "code": r.code,
                "severity": r.severity,
                "scope": r.scope,
                "title": r.title,
                "detail": r.detail,
                "context": r.context or {},
                "client_id": str(r.client_id),
                "member_name": member_name,
                "first_detected_at": r.first_detected_at.isoformat(),
            })
            if r.scope == "member":
                hh["_members"][str(r.client_id)] = member_name
            hh["_max_rank"] = max(hh["_max_rank"], _SEVERITY_RANK.get(r.severity, 0))

        # Optional search: keep households where the primary OR any warned member
        # matches the query (name or client id).
        if search:
            q = search.lower()

            def matches(hh):
                if q in hh["household_name"].lower() or q in hh["client_id"].lower():
                    return True
                for w in hh["warnings"]:
                    if q in w["member_name"].lower() or q in w["client_id"].lower():
                        return True
                return False

            households = {k: v for k, v in households.items() if matches(v)}

        ordered = sorted(
            households.values(),
            key=lambda h: (-h["_max_rank"], h["warnings"][0]["first_detected_at"]),
        )

        # Summary across the full match (not just the page).
        total = len(ordered)
        red = sum(1 for h in ordered if h["_max_rank"] == _SEVERITY_RANK[WarningSeverity.RED])
        orange = total - red
        by_code = {}
        for h in ordered:
            for w in h["warnings"]:
                by_code[w["code"]] = by_code.get(w["code"], 0) + 1

        start = (page - 1) * PAGE_SIZE
        page_rows = ordered[start:start + PAGE_SIZE]
        results = []
        for h in page_rows:
            h["affected_members"] = [
                {"client_id": cid, "name": name}
                for cid, name in h["_members"].items()
            ]
            h.pop("_members", None)
            h.pop("_max_rank", None)
            # Sort a household's own warnings red-first for display.
            h["warnings"].sort(
                key=lambda w: _SEVERITY_RANK.get(w["severity"], 0), reverse=True
            )
            results.append(h)

        return Response({
            "summary": {
                "households": total,
                "red": red,
                "orange": orange,
                "by_code": by_code,
            },
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "has_more": start + len(page_rows) < total,
            "results": results,
        })

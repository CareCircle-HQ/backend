"""Customer Service -> Care Management: households with active warnings.

Reads the persisted :class:`~api.models.MemberWarning` snapshot (kept current by
the on-open live scan, the case-save / import hooks and the nightly sweep) and
returns ONE ROW PER HOUSEHOLD so CS can work the queue of members with problems.

Detection lives in ``api.services.warnings``; this endpoint only queries the
snapshot, so it is cheap and never recomputes across the whole DB.
"""

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response

from api.models import (
    CaseMismatchFlag,
    CaseMismatchStatus,
    CaseMismatchType,
    MemberDietaryProfile,
    MemberWarning,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    WarningSeverity,
    WarningStatus,
)
from api.services.warnings import CARE_MANAGEMENT_CODES
from .base import PortalAPIView, current_agent

# Care Management is a CS queue: CS + Management (and manager override).
_ALLOWED_GROUPS = ("CS", "Management")
# Dismissing a Case Mismatch flag is a Customer-Service-only action (CS +
# Management/override); a plain Care Management viewer cannot clear the pin.
_DISMISS_GROUPS = ("CS", "Management")

_SEVERITY_RANK = {WarningSeverity.RED: 2, WarningSeverity.ORANGE: 1}

PAGE_SIZE = 25

# Safety cap on how many flagged households the list re-scans on load. The queue
# is the set of OPEN problems (self-limiting: it shrinks as CS fixes them), but
# cap it so a pathological snapshot can never blow up the request.
_RESCAN_CAP = 500


def _refresh_flagged_snapshot():
    """Re-evaluate the warnings for the currently-flagged households so anything
    an agent just fixed drops off the Care Management queue IMMEDIATELY -- not
    only after the next case-save hook or the nightly sweep. Scoped to the
    already-flagged set (the working queue) and capped. Best-effort: a failure
    must never break the list."""
    from api.models import EnrollmentVerification
    from api.services.warnings import sync_household_warnings

    try:
        enr_ids = list(
            MemberWarning.objects.filter(
                status=WarningStatus.ACTIVE, code__in=CARE_MANAGEMENT_CODES,
            )
            .exclude(enrollment__isnull=True)
            .values_list("enrollment_id", flat=True)
            .distinct()[:_RESCAN_CAP]
        )
        if not enr_ids:
            return
        for enr in EnrollmentVerification.objects.filter(pk__in=enr_ids).select_related(
            "client", "household", "kitchen"
        ):
            try:
                sync_household_warnings(enr)
            except Exception:  # pragma: no cover - one bad household can't stall the queue
                pass
    except Exception:  # pragma: no cover - defensive
        pass


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

        # Refresh the snapshot for the flagged households FIRST, so issues an
        # agent just remediated (on any path -- kitchen/cadence fix, insurance
        # update, case change) are resolved before we read + count. This is the
        # fix for warnings lingering on the queue after they're resolved.
        _refresh_flagged_snapshot()

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


# ── Case Mismatch (governing-case Household<->Individual scope switch) ─────────
def _can_dismiss(agent):
    if not agent:
        return False
    return agent.group in _DISMISS_GROUPS or getattr(agent, "is_manager", False)


def _flag_payload(flag):
    """Serialize a CaseMismatchFlag row (+ its currently-pinned members)."""
    pinned = []
    if flag.enrollment_id is not None:
        for mv in MemberDietaryProfile.objects.filter(
            enrollment_id=flag.enrollment_id, pause_locked=True
        ).select_related("client"):
            pinned.append({
                "member_id": mv.pk,
                "client_id": str(mv.client_id) if mv.client_id else None,
                "member_name": mv.member_name or _client_name(mv.client),
                "status": mv.status,
            })
    mtype = flag.mismatch_type
    return {
        "id": flag.id,
        "client_id": str(flag.client_id),
        "household_name": _client_name(flag.client),
        "enrollment_id": flag.enrollment_id,
        "mismatch_type": mtype,
        "mismatch_type_label": CaseMismatchType(mtype).label if mtype else "",
        "previous_case_id": flag.previous_case_id,
        "new_case_id": flag.new_case_id,
        "previous_household_type": flag.previous_household_type,
        "new_household_type": flag.new_household_type,
        "detail": flag.detail,
        "context": flag.context or {},
        "status": flag.status,
        "created_at": flag.created_at.isoformat(),
        "dismissed_at": flag.dismissed_at.isoformat() if flag.dismissed_at else None,
        "dismissed_by": flag.dismissed_by,
        "dismiss_reason": flag.dismiss_reason,
        "pinned_members": pinned,
    }


class CaseMismatchListView(PortalAPIView):
    """GET /portal/care-management/case-mismatch/ — governing-case scope-switch
    flags awaiting Customer Service review.

    Query params: ``status`` (open [default] | dismissed | all), ``search``
    (household name or client id), ``page``. Returns summary counts + a page.
    """

    def get(self, request):
        agent = current_agent(request)
        if not _can_access(agent):
            return Response(
                {"detail": "Care Management access required."}, status=403
            )

        params = request.query_params
        status_filter = (params.get("status") or "open").lower()
        search = (params.get("search") or "").strip()
        try:
            page = max(1, int(params.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        qs = CaseMismatchFlag.objects.select_related("client", "enrollment")
        if status_filter == "open":
            qs = qs.filter(status=CaseMismatchStatus.OPEN)
        elif status_filter == "dismissed":
            qs = qs.filter(status=CaseMismatchStatus.DISMISSED)
        if search:
            qs = qs.filter(
                Q(client__first_name__icontains=search)
                | Q(client__last_name__icontains=search)
                | Q(client__client_id__icontains=search)
            )

        # Summary counts over the full (unpaged, unfiltered-by-status) set.
        all_flags = CaseMismatchFlag.objects.all()
        open_count = all_flags.filter(status=CaseMismatchStatus.OPEN).count()
        dismissed_count = all_flags.filter(
            status=CaseMismatchStatus.DISMISSED
        ).count()

        qs = qs.order_by("-created_at")
        total = qs.count()
        start = (page - 1) * PAGE_SIZE
        rows = list(qs[start:start + PAGE_SIZE])
        results = [_flag_payload(f) for f in rows]

        return Response({
            "summary": {
                "open": open_count,
                "dismissed": dismissed_count,
            },
            "count": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "results": results,
        })


class CaseMismatchDismissView(PortalAPIView):
    """POST /portal/care-management/case-mismatch/<flag_id>/dismiss/ —
    Customer-Service-only dismissal.

    Marks the flag DISMISSED and clears the ``pause_locked`` pin on the
    household's additional members (so agents regain control; the members stay
    Paused until an agent un-pauses them). Idempotent: dismissing an already-
    dismissed flag is a no-op that still returns the flag.
    """

    def post(self, request, flag_id):
        agent = current_agent(request)
        if not _can_dismiss(agent):
            return Response(
                {"detail": "Customer Service access required to dismiss."},
                status=403,
            )
        flag = get_object_or_404(CaseMismatchFlag, pk=flag_id)
        reason = (request.data.get("reason") or "").strip()

        if flag.status != CaseMismatchStatus.DISMISSED:
            with transaction.atomic():
                flag.status = CaseMismatchStatus.DISMISSED
                flag.dismissed_at = timezone.now()
                flag.dismissed_by = agent.name if agent else ""
                flag.dismiss_reason = reason
                flag.save(update_fields=[
                    "status", "dismissed_at", "dismissed_by", "dismiss_reason",
                ])
                # Clear the pin on the household's additional members so agents
                # can un-pause them again (never auto-unpaused here).
                if flag.enrollment_id is not None:
                    MemberDietaryProfile.objects.filter(
                        enrollment_id=flag.enrollment_id, pause_locked=True
                    ).update(pause_locked=False)

        return Response(_flag_payload(flag))

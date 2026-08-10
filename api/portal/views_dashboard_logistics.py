"""Logistics dashboard analytics (Logistics / Management).

A single aggregate endpoint reporting on the fulfillment pipeline, plus a
drill-down list endpoint. Sections:

* queue      -- SNAPSHOT: households awaiting kitchen assignment, using the
  EXACT definition the Logistics page renders (``MembersListView`` scope=logistics
  + the renderable-group drop) so the count reconciles with that page; aging by
  ``stage_at``, at-risk/unassignable households (no active kitchen can serve every
  member), and PO-blocker stats (see :func:`po_blocker_stats`).
* capacity   -- SNAPSHOT: lightweight kitchen fleet status (active/inactive/
  suspended) and product coverage (how many kitchens make meals vs boxes).
  Per-kitchen daily capacity/load is intentionally not reported.
* forecast   -- FORECAST (next 7 / 14 days, ignores the date selector):
  scheduled deliveries + meals/boxes per day, plus next-7-day breakdowns by
  delivery CADENCE, product KIND (meals/boxes), KITCHEN and menu-type mix.
* kitchen_orders -- RANGE (by PurchaseOrder.delivery_date): POs by status +
  volume per kitchen, the kitchen fulfillment funnel (kitchen_status), rerouted
  deliveries, and time-to-send.
* outcomes   -- RANGE (by DeliveryOrder.expected_delivery_date): delivery status
  funnel, failed/returned + on-time rates, delivered volume; plus Out of Orbit /
  Out of Range fulfillment exclusions (snapshot).

Assignment unit is the household (enrollment). Volume is meals/boxes via
``DeliveryOrder.quantity`` / ``OrderSchedule.how_many_meals_or_boxes``.
"""

from datetime import timedelta

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from ..models import (
    Cadence,
    Client,
    DeliveryCadence,
    DeliveryOrder,
    DeliveryOrderStatus,
    EnrollmentStage,
    HouseholdMember,
    Kitchen,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    MemberStatus,
    OrderSchedule,
    OrderStatus,
    ProductTypeKind,
    ScheduleStatus,
    PurchaseOrder,
    PurchaseOrderKitchenStatus,
    PurchaseOrderStatus,
    SERVICE_EXCLUDED_ENROLLMENT_STAGES,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
)
from .serializers import active_enrollment
from ..services.catalog import product_type_kind_for_name
from ..services.po_blockers import (
    BLOCKED_REASONS,
    FIXABLE_REASONS,
    REASON_DESCRIPTIONS,
    REASON_LABELS,
    classify_po_blockers,
    summarize_po_blockers,
)
from .base import PortalAPIView, current_agent
from .views_dashboard import period_window
from .views_members import MembersListView

# A PO not yet sent whose delivery is within this many days is "at risk".
_IMMINENT_DAYS = 2

# Drill-down reasons the logistics list endpoint understands.
LOGISTICS_REASONS = frozenset({
    "awaiting", "aging_0_2", "aging_3_7", "aging_8_14", "aging_15_plus",
    "at_risk", "out_of_orbit", "out_of_range",
})


def _is_privileged(agent):
    """Logistics dashboard: Logistics + Management groups (+ manager override)."""
    if not agent:
        return False
    return (
        agent.group in ("Logistics", "Management")
        or getattr(agent, "is_manager", False)
    )


def _age_bucket(stage_at, today):
    """Aging bucket key for a household that entered the queue on ``stage_at``.
    Missing timestamps count as freshly-queued (d0_2)."""
    if stage_at is None:
        return "d0_2"
    days = (today - timezone.localtime(stage_at).date()).days
    if days <= 2:
        return "d0_2"
    if days <= 7:
        return "d3_7"
    if days <= 14:
        return "d8_14"
    return "d15_plus"


# ---------------------------------------------------------------------------
# Week-over-week PO membership diff (who we serve on a PO, by kitchen x cadence)
# ---------------------------------------------------------------------------
_WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _completed_week_ranges(today):
    """(this_start, this_end, last_start, last_end) for the two most recent
    COMPLETED Mon-Sun weeks. The current (partial) week is excluded so the two
    columns are always full, comparable weeks."""
    this_monday = today - timedelta(days=today.weekday())
    this_end = this_monday - timedelta(days=1)          # last Sunday
    this_start = this_end - timedelta(days=6)           # its Monday
    last_end = this_start - timedelta(days=1)
    last_start = last_end - timedelta(days=6)
    return this_start, this_end, last_start, last_end


def _cadence_key_label(weekday_ints):
    """Cadence key + display label derived from the weekdays a member was ACTUALLY
    on a PO that week (e.g. {0, 3} -> ('mon,thu', 'Mon / Thu')). Deriving from the
    real delivery days -- not the stored plan -- makes a cadence move visible
    across the two weeks."""
    codes = [_WEEKDAY_CODES[i] for i in sorted(set(weekday_ints)) if 0 <= i <= 6]
    if not codes:
        return "none", "Unscheduled"
    return ",".join(codes), " / ".join(c.title() for c in codes)


def _po_week_membership(start, end):
    """Distinct members ON A PO (non-cancelled DeliveryOrder) in [start, end],
    grouped into (kitchen, cadence) cells.

    Returns (cells, members, member_cells, names):
      cells        cell_key -> set(member_id)
      members      set(member_id) on ANY PO that week (new-to-service / exited test)
      member_cells member_id -> set(cell_key)  (for move detection + drill-down)
      names        member_id -> display name
    where cell_key = (kitchen_id|None, kitchen_name, cadence_key, cadence_label).
    A member delivered on two kitchens in one week appears in BOTH kitchen cells.
    """
    rows = (
        DeliveryOrder.objects
        .filter(expected_delivery_date__gte=start, expected_delivery_date__lte=end)
        .exclude(status=DeliveryOrderStatus.CANCELLED)
        .exclude(member__isnull=True)
        .values_list(
            "member_id", "member__first_name", "member__last_name",
            "kitchen_id", "kitchen__name", "expected_delivery_date",
        )
    )
    grp = {}  # (member_id, kitchen_id) -> {name, kitchen_name, weekdays:set}
    for mid, fn, ln, kid, kname, d in rows:
        g = grp.setdefault((mid, kid), {
            "name": (f"{fn or ''} {ln or ''}".strip() or str(mid)),
            "kitchen_name": kname or "Unassigned",
            "weekdays": set(),
        })
        g["weekdays"].add(d.weekday())

    cells, members, member_cells, names = {}, set(), {}, {}
    for (mid, kid), g in grp.items():
        ckey, clabel = _cadence_key_label(g["weekdays"])
        cell = (str(kid) if kid else None, g["kitchen_name"], ckey, clabel)
        cells.setdefault(cell, set()).add(mid)
        member_cells.setdefault(mid, set()).add(cell)
        members.add(mid)
        names[mid] = g["name"]
    return cells, members, member_cells, names


def po_membership_diff(today):
    """Week-over-week churn of members ON A PO, per kitchen x cadence: this vs
    last completed week, with New split into truly-new-to-service vs moved-in, and
    Dropped split into exited vs moved-out."""
    ts, te, ls, le = _completed_week_ranges(today)
    cells_t, members_t, _mc_t, _n_t = _po_week_membership(ts, te)
    cells_l, members_l, _mc_l, _n_l = _po_week_membership(ls, le)

    kitchens, cadences, out_cells = {}, {}, []
    for cell in set(cells_t) | set(cells_l):
        kid, kname, ckey, clabel = cell
        this_m = cells_t.get(cell, set())
        last_m = cells_l.get(cell, set())
        new = this_m - last_m
        dropped = last_m - this_m
        new_true = {m for m in new if m not in members_l}      # not on ANY PO last week
        exited = {m for m in dropped if m not in members_t}    # not on ANY PO this week
        kitchens[kid] = kname
        cadences[ckey] = clabel
        out_cells.append({
            "kitchen_id": kid, "kitchen_name": kname,
            "cadence_key": ckey, "cadence_label": clabel,
            "this": len(this_m), "last": len(last_m),
            "new_total": len(new), "new_true": len(new_true),
            "moved_in": len(new) - len(new_true),
            "dropped_total": len(dropped), "exited": len(exited),
            "moved_out": len(dropped) - len(exited),
        })
    return {
        "this_week": {"start": ts.isoformat(), "end": te.isoformat()},
        "last_week": {"start": ls.isoformat(), "end": le.isoformat()},
        "kitchens": [
            {"id": k, "name": v}
            for k, v in sorted(kitchens.items(), key=lambda kv: (kv[1] or "").lower())
        ],
        "cadences": [
            {"key": k, "label": v} for k, v in sorted(cadences.items())
        ],
        "cells": out_cells,
        "totals": {"this": len(members_t), "last": len(members_l)},
    }


def po_membership_cell_members(today, kitchen_id, cadence_key):
    """Drill-down: the member NAMES in each churn bucket for one (kitchen,
    cadence) cell -- new-to-service, moved-in (+ where from last week), exited,
    moved-out (+ where to this week)."""
    ts, te, ls, le = _completed_week_ranges(today)
    cells_t, members_t, mcells_t, names_t = _po_week_membership(ts, te)
    cells_l, members_l, mcells_l, names_l = _po_week_membership(ls, le)
    kid = kitchen_id or None

    def _cell_members(cells):
        out = set()
        for (c_kid, _c_kname, c_ckey, _c_clabel), ms in cells.items():
            if (c_kid or None) == kid and c_ckey == cadence_key:
                out |= ms
        return out

    this_m, last_m = _cell_members(cells_t), _cell_members(cells_l)
    new, dropped = this_m - last_m, last_m - this_m
    new_true = {m for m in new if m not in members_l}
    moved_in = new - new_true
    exited = {m for m in dropped if m not in members_t}
    moved_out = dropped - exited

    def _others(mid, mcells):
        return sorted(
            f"{kname} · {clabel}"
            for (c_kid, kname, c_ckey, clabel) in mcells.get(mid, set())
            if not ((c_kid or None) == kid and c_ckey == cadence_key)
        )

    def _rows(ids, names, mcells=None):
        out = []
        for mid in ids:
            row = {"client_id": str(mid), "name": names.get(mid) or str(mid)}
            if mcells is not None:
                row["other"] = _others(mid, mcells)
            out.append(row)
        return sorted(out, key=lambda r: r["name"].lower())

    return {
        "new_true": _rows(new_true, names_t),
        "moved_in": _rows(moved_in, names_t, mcells_l),   # where they were last week
        "exited": _rows(exited, names_l),
        "moved_out": _rows(moved_out, names_l, mcells_t),  # where they are this week
    }


def queue_group_rows():
    """The households/individuals awaiting kitchen assignment, using the EXACT
    definition the Logistics page renders (``MembersListView`` scope=logistics +
    the renderable-group drop that hides households whose every member is
    out-of-orbit or has a finished internal-service case). This is why the count
    reconciles with the Logistics page instead of the raw enrollment stage count.

    Each row: ``{key, client_id, name, stage_at, kitchen_available, blockers}``
    where ``key`` is the ``(type, id)`` group key and ``client_id`` is the
    household primary (for the member drill-down link)."""
    view = MembersListView()
    view.request = Request(
        APIRequestFactory().get("/portal/members/", {"scope": "logistics"})
    )
    view.kwargs = {}
    entries = view._group_entries()
    keys = view._renderable_keys(entries)
    entries = [e for e in entries if (e["type"], e["id"]) in keys]
    if not entries:
        return []
    # Readiness checkers (per group) -- reuses the page's serviceability calc, so
    # "kitchen_available" here is identical to the page's "Kitchen needs review".
    checks = view._compute_logistics_checks(entries)

    hh_ids = [e["id"] for e in entries if e["type"] == "household"]
    ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
    primary_by_hh = {}
    if hh_ids:
        for hm in HouseholdMember.objects.filter(
            household_id__in=hh_ids, is_primary=True
        ).select_related("client"):
            primary_by_hh[hm.household_id] = hm.client
    ind_clients = (
        {c.client_id: c for c in Client.objects.filter(client_id__in=ind_ids)}
        if ind_ids else {}
    )

    rows = []
    for e in entries:
        key = (e["type"], e["id"])
        client = (
            primary_by_hh.get(e["id"]) if e["type"] == "household"
            else ind_clients.get(e["id"])
        )
        if client is None:
            continue
        enr = active_enrollment(client)
        agg = checks.get(key, (None, {}))[1] or {}
        rows.append({
            "key": key,
            "client_id": str(client.client_id),
            "name": e["name"] or f"{client.first_name} {client.last_name}".strip(),
            "stage_at": getattr(enr, "stage_at", None),
            "kitchen_available": agg.get("kitchen_available", True),
            "blockers": agg.get("blockers", []),
        })
    return rows


def logistics_queue_rows(reason):
    """Filter :func:`queue_group_rows` to the queue-side drill-down ``reason``."""
    today = timezone.localdate()
    rows = queue_group_rows()
    if reason == "awaiting":
        return rows
    if reason == "at_risk":
        return [r for r in rows if not r["kitchen_available"]]
    if reason in ("aging_0_2", "aging_3_7", "aging_8_14", "aging_15_plus"):
        want = reason.replace("aging_", "d")
        return [r for r in rows if _age_bucket(r["stage_at"], today) == want]
    return []


def po_blocker_stats():
    """Per-reason PO-blocker counts + metadata (mirrors POBlockersStatsView) for
    the At-Risk / Unassignable section: active members with a live delivery plan
    that can't reach a Purchase Order."""
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
        if counts.get(r, 0) > 0
    ]
    reasons.sort(key=lambda x: -x["count"])
    return {"total": len(rows), "reasons": reasons}


class LogisticsDashboardView(PortalAPIView):
    """Aggregate fulfillment-pipeline analytics. See module docstring."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )

        period = (request.query_params.get("period") or "all").lower()
        start, end = period_window(period)
        today = timezone.localdate()

        return Response({
            "period": period,
            "range": (
                {"start": start.isoformat(), "end": end.isoformat()}
                if start is not None else None
            ),
            "queue": self._queue(today),
            "capacity": self._capacity(today),
            "forecast": self._forecast(today),
            "po_membership_diff": po_membership_diff(today),
            "kitchen_orders": self._kitchen_orders(start, end),
            "outcomes": self._outcomes(start, end),
        })

    # -- Section 1: assignment queue (snapshot) ------------------------------
    def _queue(self, today):
        rows = queue_group_rows()
        aging = {"d0_2": 0, "d3_7": 0, "d8_14": 0, "d15_plus": 0}
        at_risk = 0
        for r in rows:
            aging[_age_bucket(r["stage_at"], today)] += 1
            if not r["kitchen_available"]:
                at_risk += 1
        return {
            "awaiting": len(rows),
            "aging": aging,
            "at_risk": at_risk,
            "po_blockers": po_blocker_stats(),
        }

    # -- Section 2: kitchen fleet (snapshot) --------------------------------
    def _capacity(self, today):
        # Per-kitchen daily capacity/load is intentionally not reported here.
        # We keep only the lightweight fleet snapshot: how many kitchens are
        # active/inactive/suspended and how many support meals vs boxes.
        status_counts = {"active": 0, "inactive": 0, "suspended": 0}
        meals_kitchens = boxes_kitchens = 0
        for k in Kitchen.objects.all():
            status_counts[k.status] = status_counts.get(k.status, 0) + 1
            products = k.supported_products or []
            if "meal" in products:
                meals_kitchens += 1
            if "box" in products:
                boxes_kitchens += 1
        return {
            "status_counts": status_counts,
            "product_coverage": {"meals": meals_kitchens, "boxes": boxes_kitchens},
        }

    # -- Section 3: production forecast (fixed forward window) ---------------
    def _forecast(self, today):
        end14 = today + timedelta(days=13)
        # Per-day totals split Meals/Boxes. Group by (date, program_name) so we
        # classify the kind on a small number of rows.
        rows = (
            OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                anticipated_delivery_date__lte=end14,
            )
            .values("anticipated_delivery_date", "program_name")
            .annotate(deliveries=Count("order_id"), qty=Sum("how_many_meals_or_boxes"))
        )
        kind_cache = {}

        def kind_of(name):
            if name not in kind_cache:
                kind_cache[name] = product_type_kind_for_name(name)
            return kind_cache[name]

        by_date = {}
        for r in rows:
            d = r["anticipated_delivery_date"].isoformat()
            bucket = by_date.setdefault(
                d, {"date": d, "meals": 0, "boxes": 0, "deliveries": 0}
            )
            qty = r["qty"] or 0
            bucket["deliveries"] += r["deliveries"]
            if kind_of(r["program_name"]) == ProductTypeKind.BOXES:
                bucket["boxes"] += qty
            else:
                bucket["meals"] += qty
        days = [by_date.get((today + timedelta(days=i)).isoformat(),
                             {"date": (today + timedelta(days=i)).isoformat(),
                              "meals": 0, "boxes": 0, "deliveries": 0})
                for i in range(14)]

        def totals(day_slice):
            return {
                "meals": sum(d["meals"] for d in day_slice),
                "boxes": sum(d["boxes"] for d in day_slice),
                "deliveries": sum(d["deliveries"] for d in day_slice),
            }

        end7 = today + timedelta(days=6)
        # Breakdowns over the next 7 days, split by delivery CADENCE, product
        # KIND (meals/boxes) and KITCHEN. Cadence is not stored on OrderSchedule,
        # so we join each occurrence to the member's delivery PLAN
        # (MemberDeliverySchedule) via (enrollment_id, member_profile_id).
        orders7 = list(
            OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
                anticipated_delivery_date__lte=end7,
            ).values(
                "enrollment_id", "member_id", "program_name",
                "kitchen_id", "kitchen__name", "menu_type",
                "how_many_meals_or_boxes",
            )
        )
        cadence_of = {
            (r["enrollment_id"], r["member_profile_id"]): r["delivery_days_cadence"]
            for r in MemberDeliverySchedule.objects.filter(
                enrollment_id__in={o["enrollment_id"] for o in orders7}
            ).values("enrollment_id", "member_profile_id", "delivery_days_cadence")
        }
        # Labels: legacy enum + any configurable Cadence rows (by code).
        cadence_labels = dict(DeliveryCadence.choices)
        cadence_labels.update(
            {c.code: c.label for c in Cadence.objects.all() if c.code}
        )

        def cadence_label(code):
            if not code:
                return "Unassigned"
            return cadence_labels.get(code) or code.replace("_", " ").title()

        by_cadence, by_kitchen_map, menu_map = {}, {}, {}
        for o in orders7:
            qty = o["how_many_meals_or_boxes"] or 0
            is_box = kind_of(o["program_name"]) == ProductTypeKind.BOXES
            code = cadence_of.get((o["enrollment_id"], o["member_id"])) or ""
            cb = by_cadence.setdefault(code, {"meals": 0, "boxes": 0, "deliveries": 0})
            kid = str(o["kitchen_id"]) if o["kitchen_id"] else None
            kb = by_kitchen_map.setdefault(
                kid,
                {"id": kid, "name": o["kitchen__name"] or "Unassigned",
                 "meals": 0, "boxes": 0, "deliveries": 0},
            )
            mt = (o["menu_type"] or "").strip() or "—"
            mm = menu_map.setdefault(
                mt, {"code": mt, "meals": 0, "boxes": 0, "deliveries": 0}
            )
            for bucket in (cb, kb, mm):
                bucket["deliveries"] += 1
                bucket["boxes" if is_box else "meals"] += qty

        by_cadence_list = sorted(
            (
                {
                    "code": code or "unknown",
                    "label": cadence_label(code),
                    "meals": v["meals"], "boxes": v["boxes"],
                    "deliveries": v["deliveries"],
                }
                for code, v in by_cadence.items()
            ),
            key=lambda r: -r["deliveries"],
        )
        by_kitchen = sorted(by_kitchen_map.values(), key=lambda r: -r["deliveries"])[:10]
        menu_mix = sorted(menu_map.values(), key=lambda r: -r["deliveries"])[:8]

        return {
            "days": days,
            "next_7": totals(days[:7]),
            "next_14": totals(days),
            "by_cadence": by_cadence_list,
            "menu_mix": menu_mix,
            "by_kitchen": by_kitchen,
        }

    # -- Section 4: kitchen orders (range by delivery_date) ------------------
    def _kitchen_orders(self, start, end):
        po = PurchaseOrder.objects.all()
        do = DeliveryOrder.objects.all()
        if start is not None:
            po = po.filter(delivery_date__gte=start, delivery_date__lte=end)
            do = do.filter(
                expected_delivery_date__gte=start, expected_delivery_date__lte=end
            )

        po_status = {
            row["status"]: row["n"]
            for row in po.values("status").annotate(n=Count("purchase_order_id"))
        }
        kitchen_funnel = {
            row["kitchen_status"]: row["n"]
            for row in po.values("kitchen_status").annotate(n=Count("purchase_order_id"))
        }

        # Volume + PO count per kitchen (top by volume).
        vol_by_kitchen = {
            str(r["kitchen_id"]): (r["qty"] or 0)
            for r in do.exclude(kitchen__isnull=True)
            .values("kitchen_id")
            .annotate(qty=Sum("quantity"))
        }
        po_by_kitchen = {
            str(r["kitchen_id"]): r["n"]
            for r in po.exclude(kitchen__isnull=True)
            .values("kitchen_id")
            .annotate(n=Count("purchase_order_id"))
        }
        names = {
            str(k.pk): k.name
            for k in Kitchen.objects.filter(
                pk__in=set(vol_by_kitchen) | set(po_by_kitchen)
            )
        }
        per_kitchen = sorted(
            (
                {
                    "id": kid,
                    "name": names.get(kid, "—"),
                    "pos": po_by_kitchen.get(kid, 0),
                    "volume": vol_by_kitchen.get(kid, 0),
                }
                for kid in set(vol_by_kitchen) | set(po_by_kitchen)
            ),
            key=lambda r: -r["volume"],
        )[:10]

        # At-risk: not yet sent to the kitchen with an imminent delivery date.
        today = timezone.localdate()
        at_risk = po.filter(
            kitchen_status=PurchaseOrderKitchenStatus.NOT_SENT,
            delivery_date__isnull=False,
            delivery_date__lte=today + timedelta(days=_IMMINENT_DAYS),
        ).count()

        # Rerouted deliveries: actual kitchen differs from the default.
        rerouted = (
            do.exclude(kitchen__isnull=True)
            .exclude(default_kitchen__isnull=True)
            .exclude(kitchen=F("default_kitchen"))
            .count()
        )

        # Time-to-send (hours): sent_to_kitchen_at - created_at over sent POs.
        sent = po.filter(sent_to_kitchen_at__isnull=False).annotate(
            lead=ExpressionWrapper(
                F("sent_to_kitchen_at") - F("created_at"), output_field=DurationField()
            )
        )
        avg_lead = sent.aggregate(a=Avg("lead"))["a"]
        avg_send_hours = round(avg_lead.total_seconds() / 3600.0, 1) if avg_lead else 0.0

        return {
            "total_pos": po.count(),
            "total_volume": do.aggregate(q=Sum("quantity"))["q"] or 0,
            "po_status": {
                "draft": po_status.get(PurchaseOrderStatus.DRAFT, 0),
                "confirmed": po_status.get(PurchaseOrderStatus.CONFIRMED, 0),
                "completed": po_status.get(PurchaseOrderStatus.COMPLETED, 0),
                "cancelled": po_status.get(PurchaseOrderStatus.CANCELLED, 0),
            },
            "kitchen_funnel": {
                "not_sent": kitchen_funnel.get(PurchaseOrderKitchenStatus.NOT_SENT, 0),
                "sent": kitchen_funnel.get(PurchaseOrderKitchenStatus.SENT_TO_KITCHEN, 0),
                "accepted": kitchen_funnel.get(
                    PurchaseOrderKitchenStatus.ACCEPTED_BY_KITCHEN, 0
                ),
                "in_preparation": kitchen_funnel.get(
                    PurchaseOrderKitchenStatus.IN_PREPARATION, 0
                ),
                "ready_for_dispatch": kitchen_funnel.get(
                    PurchaseOrderKitchenStatus.READY_FOR_DISPATCH, 0
                ),
            },
            "per_kitchen": per_kitchen,
            "at_risk_not_sent": at_risk,
            "rerouted": rerouted,
            "avg_send_hours": avg_send_hours,
        }

    # -- Section 5: delivery outcomes & exclusions ---------------------------
    def _outcomes(self, start, end):
        do = DeliveryOrder.objects.all()
        if start is not None:
            do = do.filter(
                expected_delivery_date__gte=start, expected_delivery_date__lte=end
            )
        status_counts = {
            row["status"]: row["n"]
            for row in do.values("status").annotate(n=Count("delivery_order_id"))
        }
        delivered = status_counts.get(DeliveryOrderStatus.DELIVERED, 0)
        failed = status_counts.get(DeliveryOrderStatus.FAILED, 0)
        returned = status_counts.get(DeliveryOrderStatus.RETURNED, 0)
        total = sum(status_counts.values())

        # On-time: delivered on/before the expected date.
        on_time = do.filter(
            status=DeliveryOrderStatus.DELIVERED,
            delivered_at__isnull=False,
            delivered_at__date__lte=F("expected_delivery_date"),
        ).count()
        delivered_volume = (
            do.filter(status=DeliveryOrderStatus.DELIVERED).aggregate(
                q=Sum("quantity")
            )["q"]
            or 0
        )

        # Exclusions (snapshot, distinct members): dietary + geographic blocks.
        mdp = MemberDietaryProfile.objects
        out_of_orbit = (
            mdp.filter(status=MemberStatus.OUT_OF_ORBIT)
            .values("client_id").distinct().count()
        )
        out_of_range = (
            mdp.filter(status=MemberStatus.OUT_OF_RANGE)
            .values("client_id").distinct().count()
        )

        return {
            "total": total,
            "status": {
                "pending": status_counts.get(DeliveryOrderStatus.PENDING, 0),
                "ready": status_counts.get(DeliveryOrderStatus.READY_FOR_DELIVERY, 0),
                "out": status_counts.get(DeliveryOrderStatus.OUT_FOR_DELIVERY, 0),
                "delivered": delivered,
                "failed": failed,
                "returned": returned,
                "cancelled": status_counts.get(DeliveryOrderStatus.CANCELLED, 0),
                "on_hold": status_counts.get(DeliveryOrderStatus.ON_HOLD, 0),
            },
            "delivered": delivered,
            "delivered_volume": delivered_volume,
            "failed_returned": failed + returned,
            "failed_rate": (
                round((failed + returned) / total * 100, 1) if total else 0.0
            ),
            "on_time": on_time,
            "on_time_rate": round(on_time / delivered * 100, 1) if delivered else 0.0,
            "exclusions": {"out_of_orbit": out_of_orbit, "out_of_range": out_of_range},
        }


class LogisticsDashboardListView(PortalAPIView):
    """Drill-down: the members/households behind one logistics ``reason`` (see
    :data:`LOGISTICS_REASONS`). Each row links to the member profile."""

    def get(self, request, reason):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )
        if reason not in LOGISTICS_REASONS:
            return Response({"detail": "Unknown reason."}, status=404)

        today = timezone.localdate()

        if reason in ("out_of_orbit", "out_of_range"):
            status = (
                MemberStatus.OUT_OF_ORBIT if reason == "out_of_orbit"
                else MemberStatus.OUT_OF_RANGE
            )
            rows = (
                MemberDietaryProfile.objects.filter(status=status)
                .select_related("client")[:200]
            )
            results = []
            for m in rows:
                cid = str(m.client_id) if m.client_id else str(m.pk)
                results.append({
                    "id": cid,
                    "name": (m.member_name or cid),
                    "detail": (m.menu_type or "").strip() or "—",
                })
            results.sort(key=lambda r: r["name"].lower())
            return Response({"reason": reason, "count": len(results), "results": results})

        # Queue-side reasons (household-based; mirrors the Logistics page).
        rows = logistics_queue_rows(reason)
        rows.sort(key=lambda r: (r["stage_at"] is not None, r["stage_at"] or today))
        results = []
        for r in rows[:200]:
            days = (
                (today - timezone.localtime(r["stage_at"]).date()).days
                if r["stage_at"] else 0
            )
            detail = f"Waiting {days} day{'s' if days != 1 else ''}"
            if reason == "at_risk" and r["blockers"]:
                detail += " · " + ", ".join(r["blockers"])
            results.append({"id": r["client_id"], "name": r["name"], "detail": detail})
        return Response({"reason": reason, "count": len(results), "results": results})


def _plan_is_box(ptype, program_name, kitchen_products):
    """Meals-vs-boxes for a delivery plan, matching the precedence Purchase Order
    generation uses (``_due_schedules``): the PROGRAM NAME is authoritative.

    1. A meal/box keyword in the program name (``product_type_kind_for_name``) --
       same as the PO resolver, and the source of truth an agent sees.
    2. Fallback to the plan's ``product_type.type`` snapshot when the program name
       carries no keyword.
    3. Last resort: the kitchen fleet (box-only kitchen => boxes, else meals).

    Trusting ``product_type.type`` first was wrong: some Meals-program plans carry
    a stale ``boxes`` snapshot, which mislabeled real meal clients as boxes.
    Never infers boxes from missing data."""
    kind = product_type_kind_for_name(program_name)
    if kind is not None:
        return kind == ProductTypeKind.BOXES
    if ptype:
        return ptype == ProductTypeKind.BOXES
    return "box" in (kitchen_products or []) and "meal" not in (kitchen_products or [])


def _distribution_plan_qs(scope, today):
    """Base MemberDeliverySchedule queryset for the distribution views, scoped to
    ``active`` or ``all``.

    ``active`` = any live (SCHEDULED) plan that hasn't ended yet, INCLUDING plans
    whose first delivery is still upcoming. A household assigned to a kitchen is
    an active assignment the moment it's set up, even if its first delivery date
    is next week -- so we intentionally do NOT require ``starts_on <= today``
    (that would hide freshly-assigned kitchens until their window opened).

    ``active`` also applies the SAME exclusions Purchase Order generation uses, so
    the counts reflect who would actually be served: households On Hold or in a
    terminal stage (``SERVICE_EXCLUDED_ENROLLMENT_STAGES``) and Paused / Out of
    Orbit / Out of Range / Inactive members (``SERVICE_EXCLUDED_MEMBER_STATUSES``)
    are dropped. Households still in ``KITCHEN_ASSIGNMENT`` (waiting to be assigned
    a kitchen) are also dropped: they aren't distributed yet, so counting them
    only inflated the Unassigned bucket. ``all`` is the unfiltered superset (every
    plan, any status/date) for auditing."""
    qs = MemberDeliverySchedule.objects.all()
    if scope == "active":
        qs = (
            qs.filter(status=ScheduleStatus.SCHEDULED)
            .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=today))
            .exclude(enrollment__stage__in=SERVICE_EXCLUDED_ENROLLMENT_STAGES)
            .exclude(enrollment__stage=EnrollmentStage.KITCHEN_ASSIGNMENT)
            .exclude(member_profile__status__in=SERVICE_EXCLUDED_MEMBER_STATUSES)
        )
    return qs


class DistributionOverviewView(PortalAPIView):
    """Distribution Overview (Logistics / Management).

    A CADENCE x KITCHEN matrix of how many clients are set up in each delivery
    cadence at each kitchen, split by product KIND (meals/boxes). Source of truth
    is the per-member delivery PLAN (:class:`MemberDeliverySchedule`): cadence is
    ``delivery_days_cadence`` and the kitchen is the household's LIVE assignment
    (``enrollment.kitchen``), so reassignments are reflected immediately.

    Query param ``scope``:
    * ``active`` (default) -- only ``SCHEDULED`` plans whose window covers today.
    * ``all``              -- every plan regardless of status/window.

    The response lists ALL cadences (the configurable :class:`Cadence` table plus
    any codes present in the data) and ALL kitchens (with fleet status + product
    coverage), including zero-count ones, plus ``Unassigned`` row/column when the
    data has plans with no cadence / no kitchen. Counts are DISTINCT clients
    (member profiles) per cell.
    """

    UNASSIGNED = "unassigned"

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )

        scope = (request.query_params.get("scope") or "active").lower()
        if scope not in ("active", "all"):
            scope = "active"
        today = timezone.localdate()

        qs = _distribution_plan_qs(scope, today)

        # Kitchen fleet products, used as a last-resort kind hint below.
        kitchen_products = {
            str(k.pk): (k.supported_products or [])
            for k in Kitchen.objects.all()
        }

        grouped = qs.values(
            "delivery_days_cadence",
            "enrollment__kitchen_id",
            "enrollment__kitchen__name",
            "product_type__type",
            # The ENROLLMENT's program is authoritative (matches the client
            # profile + PO resolver); the schedule's own ``program`` snapshot can
            # be stale (e.g. a boxes plan left behind after the case became meals),
            # which mislabeled clients. Fall back to it only when the enrollment
            # carries no program name.
            "enrollment__program_name",
            "program__name",
        ).annotate(n=Count("member_profile_id", distinct=True))

        # cell key -> {meals, boxes, total}; track which cadences/kitchens appear.
        cells = {}
        cadence_present = set()
        kitchen_present = set()

        def bump(cell, is_box, n):
            cell["boxes" if is_box else "meals"] += n
            cell["total"] += n

        for row in grouped:
            code = row["delivery_days_cadence"] or ""
            kid = row["enrollment__kitchen_id"]
            kkey = str(kid) if kid else self.UNASSIGNED
            is_box = _plan_is_box(
                row["product_type__type"],
                row["enrollment__program_name"] or row["program__name"],
                kitchen_products.get(kkey, []),
            )
            n = row["n"] or 0
            cadence_present.add(code)
            kitchen_present.add(kkey)
            cell = cells.setdefault(
                f"{code or self.UNASSIGNED}|{kkey}",
                {"meals": 0, "boxes": 0, "total": 0},
            )
            bump(cell, is_box, n)

        # -- Cadence rows: all configurable cadences + any codes seen in data. ---
        cadence_rows = []
        seen_codes = set()
        for c in Cadence.objects.all().order_by("-is_active", "label"):
            if not c.code:
                continue
            cadence_rows.append(
                {"code": c.code, "label": c.label or c.code, "is_active": c.is_active}
            )
            seen_codes.add(c.code)
        legacy_labels = dict(DeliveryCadence.choices)
        for code in sorted(cadence_present):
            if not code or code in seen_codes:
                continue
            cadence_rows.append({
                "code": code,
                "label": legacy_labels.get(code) or code.replace("_", " ").title(),
                "is_active": True,
            })
            seen_codes.add(code)
        if "" in cadence_present:
            cadence_rows.append(
                {"code": self.UNASSIGNED, "label": "Unassigned", "is_active": True}
            )

        # -- Kitchen columns: every kitchen + fleet context, then Unassigned. ---
        kitchen_cols = []
        for k in Kitchen.objects.all().order_by("name"):
            kitchen_cols.append({
                "key": str(k.pk),
                "name": k.name,
                "status": k.status,
                "products": k.supported_products or [],
            })
        if self.UNASSIGNED in kitchen_present:
            kitchen_cols.append({
                "key": self.UNASSIGNED,
                "name": "Unassigned",
                "status": None,
                "products": [],
            })

        # -- Totals (row / column / grand). -------------------------------------
        def zero():
            return {"meals": 0, "boxes": 0, "total": 0}

        row_totals = {r["code"]: zero() for r in cadence_rows}
        col_totals = {c["key"]: zero() for c in kitchen_cols}
        grand = zero()
        for row in cadence_rows:
            for col in kitchen_cols:
                cell = cells.get(f"{row['code']}|{col['key']}")
                if not cell:
                    continue
                for field in ("meals", "boxes", "total"):
                    row_totals[row["code"]][field] += cell[field]
                    col_totals[col["key"]][field] += cell[field]
                    grand[field] += cell[field]

        return Response({
            "scope": scope,
            "cadences": cadence_rows,
            "kitchens": kitchen_cols,
            "cells": cells,
            "row_totals": row_totals,
            "col_totals": col_totals,
            "grand_total": grand,
            # Week-over-week churn of members actually ON A PO (not the roster
            # above), by kitchen x cadence -- New (new-to-service / moved-in) and
            # Dropped (exited / moved-out) between the two most recent completed
            # weeks. Reconstructed from DeliveryOrder history.
            "po_membership_diff": po_membership_diff(timezone.localdate()),
        })


class DistributionKitchenMembersView(PortalAPIView):
    """Drill-down for the Distribution Overview: the members (clients) assigned
    to one kitchen, ordered by cadence then name, paginated for lazy loading.

    ``GET /portal/dashboard/distribution/<kitchen>/members/`` where ``kitchen``
    is a Kitchen pk or the literal ``unassigned``. Query params:
    * ``scope``  -- ``active`` (default) | ``all`` (mirrors the matrix).
    * ``page``   -- 1-based page number (default 1).
    * ``search`` -- case-insensitive name filter.

    Each row carries the Client id (for the CRM deep-link), name, cadence and the
    resolved meals/boxes kind. Grouping by cadence is done client-side; ordering
    by cadence keeps groups contiguous across pages.
    """

    PAGE_SIZE = 100

    def get(self, request, kitchen):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )

        scope = (request.query_params.get("scope") or "active").lower()
        if scope not in ("active", "all"):
            scope = "active"
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        search = (request.query_params.get("search") or "").strip()
        kind_filter = (request.query_params.get("kind") or "").lower()
        if kind_filter not in ("meals", "boxes"):
            kind_filter = ""
        # Optional cadence filter. The literal "unassigned" selects plans with no
        # delivery cadence set (the matrix's Unassigned cadence row).
        cadence_filter = (request.query_params.get("cadence") or "").strip()
        today = timezone.localdate()

        # Kitchen fleet products keyed by pk, used to classify meals/boxes per row
        # (needed when kitchen == "all", where rows span kitchens).
        kitchen_products_map = {
            str(k.pk): (k.supported_products or []) for k in Kitchen.objects.all()
        }

        qs = _distribution_plan_qs(scope, today)
        if kitchen == "all":
            kitchen_name = "All kitchens"
        elif kitchen == "unassigned":
            qs = qs.filter(enrollment__kitchen__isnull=True)
            kitchen_name = "Unassigned"
        else:
            k = Kitchen.objects.filter(pk=kitchen).first()
            if k is None:
                return Response({"detail": "Kitchen not found."}, status=404)
            qs = qs.filter(enrollment__kitchen_id=kitchen)
            kitchen_name = k.name

        if cadence_filter == "unassigned":
            qs = qs.filter(delivery_days_cadence="")
        elif cadence_filter:
            qs = qs.filter(delivery_days_cadence=cadence_filter)

        if search:
            qs = qs.filter(
                Q(member_name__icontains=search)
                | Q(member_profile__client__first_name__icontains=search)
                | Q(member_profile__client__last_name__icontains=search)
            )

        cadence_labels = dict(DeliveryCadence.choices)
        cadence_labels.update(
            {c.code: c.label for c in Cadence.objects.all() if c.code}
        )

        def cadence_label(code):
            if not code:
                return "Unassigned"
            return cadence_labels.get(code) or code.replace("_", " ").title()

        rows = qs.order_by(
            "enrollment__kitchen__name", "delivery_days_cadence", "member_name"
        ).values(
            "member_profile__client_id",
            "member_profile__client__first_name",
            "member_profile__client__last_name",
            "member_name",
            "delivery_days_cadence",
            "product_type__type",
            "enrollment__program_name",
            "program__name",
            "enrollment__kitchen_id",
            "enrollment__kitchen__name",
        )

        def to_row(r):
            first = (r["member_profile__client__first_name"] or "").strip()
            last = (r["member_profile__client__last_name"] or "").strip()
            name = (f"{first} {last}".strip()) or r["member_name"] or "Unknown"
            code = r["delivery_days_cadence"] or ""
            kid = r["enrollment__kitchen_id"]
            # Enrollment program is authoritative; schedule snapshot is fallback.
            is_box = _plan_is_box(
                r["product_type__type"],
                r["enrollment__program_name"] or r["program__name"],
                kitchen_products_map.get(str(kid), []) if kid else [],
            )
            return {
                "client_id": str(r["member_profile__client_id"])
                if r["member_profile__client_id"] else None,
                "name": name,
                "cadence_code": code or "unassigned",
                "cadence_label": cadence_label(code),
                "kitchen_name": r["enrollment__kitchen__name"] or "Unassigned",
                "kind": "boxes" if is_box else "meals",
            }

        start = (page - 1) * self.PAGE_SIZE
        if kind_filter:
            # meals/boxes can't be filtered purely in SQL (the classifier falls
            # back to the program name + kitchen fleet), so classify the kitchen's
            # full ordered set and paginate the matching subset in Python. Kitchen
            # row counts are bounded, so this stays cheap.
            matched = [row for row in map(to_row, rows) if row["kind"] == kind_filter]
            total = len(matched)
            results = matched[start:start + self.PAGE_SIZE]
        else:
            total = rows.count()
            results = [to_row(r) for r in rows[start:start + self.PAGE_SIZE]]

        return Response({
            "kitchen": kitchen,
            "kitchen_name": kitchen_name,
            "scope": scope,
            "kind": kind_filter,
            "page": page,
            "page_size": self.PAGE_SIZE,
            "total": total,
            "has_more": start + len(results) < total,
            "results": results,
        })


class DistributionPoDiffMembersView(PortalAPIView):
    """Drill-down for the Distribution Overview's week-over-week PO membership
    diff: the member names in each churn bucket for one (kitchen, cadence) cell.

    ``GET /portal/dashboard/distribution/po-diff/members/?kitchen_id=<id|blank>&cadence_key=<mon,thu>``
    Returns ``{new_true, moved_in, exited, moved_out}`` -- each a list of
    ``{client_id, name[, other]}`` (``other`` = the member's other kitchen/cadence
    cells, for movers).
    """

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Logistics dashboard access required."}, status=403
            )
        kitchen_id = (request.query_params.get("kitchen_id") or "").strip() or None
        cadence_key = (request.query_params.get("cadence_key") or "").strip()
        return Response(
            po_membership_cell_members(timezone.localdate(), kitchen_id, cadence_key)
        )

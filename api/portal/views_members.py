"""Member-scoped portal endpoints: list, detail, and the profile sub-tabs
(insurance, social coverage, history, orders, household, notes, tickets) plus
the verification wizard write."""

import uuid
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    Address,
    Case,
    CaseStatus,
    CaseType,
    Client,
    ClientPhone,
    ClientPhoneSource,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    KitchenProductType,
    MemberStatus,
    MenuType,
    Note,
    NoteSource,
    ProductTypeKind,
    PurchaseOrder,
    ServiceAuthorizationStatus,
    StageEvent,
    Ticket,
    TimelineEvent,
)
from ..views_phones import _phone_dict
from ..services.catalog import menu_type_for_member, product_type_kind_for_name
from ..services.delivery import (
    cadence_options_for_kind,
    create_member_delivery_schedules,
    current_household_cadence,
    update_household_cadence,
)
from ..services.client_diagnostic import diagnose_client
from ..services.orders import (
    _format_address,
    generate_delivery_calendar,
    resync_scheduled_orders,
    sync_delivery_calendar,
)
from ..services.kitchens import (
    kitchen_offered_menu_index,
    kitchen_options,
    required_product_for_program,
    serving_kitchens_for_member,
)
from ..services.meal_rules import reconcile_member_kitchen_output, resolve_kitchen_meal
from ..services.lifecycle import InvalidTransition, advance_enrollment
from ..services import timeline
from ..serializers import (
    add_client_to_household,
    ensure_household_with_primary,
    search_clients,
    sync_household_members,
)
from .base import PortalAPIView, PortalGenericAPIView, current_agent
from . import serializers as s

# System note left on a member when their menu type / dietary needs can't be
# fulfilled by any (or the assigned) kitchen and they're pulled Out of Orbit.
NO_KITCHEN_OUT_OF_ORBIT_NOTE = (
    "No kitchen currently supports this member's dietary needs."
)

# Reverse of serializers._STATUS_MAP: a filter value -> the lifecycle stages it covers.
# Verification is a yes/no fact (Pending Verification / Verified), so those two
# chips are NOT in this map -- they are resolved via verification_completed_q()
# (the verified_at fact), not lifecycle_stage. Authorization is a separate
# dimension handled by the `authorization` filter param.
STATUS_TO_STAGES = {
    "Denied": ["not_eligible"],
    "Kitchen Assignment": ["kitchen_assignment"],
    "Active": ["active"],
    "Completed": ["completed"],
}

# Authorization filter value -> (matching statuses, statuses that OUTRANK it).
# A client is shown under a given authorization only when their GOVERNING
# internal-service case has it -- i.e. they hold a case with a matching status
# and none with a more favorable one. This mirrors lifecycle.governing_case_key
# favorability (approved/not_required > pending > denied), so the filter agrees
# with the Authorization badge (which reflects the governing case). Without the
# outrank exclusion, a client with both a denied and a pending case would wrongly
# appear under "Denied" while their badge reads "Waiting Authorization".
AUTHORIZATION_FILTERS = {
    "approved": (["approved", "not_required"], []),
    "pending": (["pending"], ["approved", "not_required"]),
    "denied": (["denied"], ["approved", "not_required", "pending"]),
}


def apply_authorization_filter(qs, value):
    """Restrict ``qs`` to clients whose GOVERNING internal-service case has the
    given authorization ``value``. Caller handles ``.distinct()``."""
    spec = AUTHORIZATION_FILTERS.get(value)
    if not spec:
        return qs
    match_statuses, outrank = spec
    qs = qs.filter(
        cases__case_type=CaseType.INTERNAL_SERVICE,
        cases__service_authorization_status__in=match_statuses,
    )
    if outrank:
        # Drop clients holding a more favorable internal-service authorization
        # (that more favorable case would be the governing one instead).
        qs = qs.exclude(
            cases__case_type=CaseType.INTERNAL_SERVICE,
            cases__service_authorization_status__in=outrank,
        )
    return qs


# Page-level base scope: restricts the list to the lifecycle stages a given
# work area cares about (independent of the per-status filter chips).
SCOPE_TO_STAGES = {
    # Verification work area: households whose verification was requested
    # (pending_verification) or completed (verified). An approved household
    # advances to kitchen_assignment and moves to the logistics work area.
    "verification": ["pending_verification", "verified"],
    "logistics": ["kitchen_assignment"],
}

# select_related the requested_by / verified_by agents on the enrollment prefetch
# so the Verification list's agent columns don't trigger an extra query per row.
MEMBER_LIST_PREFETCH = (
    "insurances",
    "military_profile",
    Prefetch(
        "enrollments",
        queryset=EnrollmentVerification.objects.select_related(
            "requested_by", "verified_by"
        ),
    ),
    "member_profiles",
)


def require_internal_service_primary(qs):
    """Restrict a Client queryset to the members the Verification page should
    show: everyone must belong to a household whose PRIMARY member holds an
    Internal Service case (the case the verification + meal/box delivery attach
    to). The internal-service-case holder is always the household primary, so
    dependents are kept via their household and strays with no household — or
    whose primary has no internal-service case — are dropped.

    Caller is responsible for ``.distinct()`` (this adds multi-valued joins)."""
    return qs.filter(
        household_membership__household__members__is_primary=True,
        household_membership__household__members__client__cases__case_type=(
            CaseType.INTERNAL_SERVICE
        ),
    )


def verification_completed_q():
    """Clients whose verification POP-UP was completed: a governing enrollment --
    their own or their household's -- has ``verified_at`` set. DB-level mirror of
    ``lifecycle.verification_completed`` and the single determinant for the
    Verification page's Pending vs Verified split.

    Keyed off the explicit verification fact, NOT the enrollment stage or the
    client's lifecycle_stage. The case authorization status (a separate
    dimension) never affects this.

    Caller is responsible for ``.distinct()`` (this adds multi-valued joins)."""
    return Q(enrollments__verified_at__isnull=False) | Q(
        household_membership__household__enrollment_verifications__verified_at__isnull=False
    )


def verification_scope_q():
    """Base scope for the Verification page, keyed off the verification FACT so
    the list reads as a full verification history: households still awaiting
    verification (lifecycle ``pending_verification``) OR that have EVER been
    verified (``verified_at`` set on a governing enrollment) -- kept even after
    they advance to kitchen assignment / active / completed. The ``verified_at``
    join is multi-valued, so the caller must ``.distinct()``."""
    return Q(lifecycle_stage="pending_verification") | verification_completed_q()


_ALLERGY_LABELS = dict(FoodAllergy.choices)


def _allergy_labels(codes):
    """Human labels for a member's food-allergy codes, dropping the no-op 'none'."""
    return [_ALLERGY_LABELS.get(c, c) for c in (codes or []) if c and c != "none"]


def predict_member_out_of_orbit(profile):
    """Predict whether a member will be Out of Orbit once a kitchen is assigned,
    from the GLOBAL meal rule + data completeness. Kitchen-agnostic (no kitchen
    is assigned yet at the Logistics stage). Returns ``(out: bool, reason: str)``.

    A member is predicted Out of Orbit when they have no menu type yet, or when
    their menu type + food allergies can't be safely fulfilled by the meal rule
    (see api.services.meal_rules). Kitchen-coverage is a separate, household-level
    check (whether ANY kitchen can serve everyone)."""
    if profile is None or not (profile.menu_type or "").strip():
        return True, "No menu type assigned"
    rule = resolve_kitchen_meal(profile.menu_type, profile.food_allergies)
    if rule.out_of_orbit:
        return True, "Menu + allergies can't be safely fulfilled"
    return False, ""


def _parse_date(value):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def period_date_range(period):
    """Map a Verification-page period code to an inclusive (start, end) date
    window on the LOCAL calendar, or None for "all"/blank/unknown (no filter).

    Weeks start on Monday. Current periods (this_week/month/year) end today
    rather than at the calendar boundary -- records can't be in the future, so
    this is equivalent and avoids surprising empty ranges.
    """
    period = (period or "").strip().lower()
    if not period or period == "all":
        return None
    today = timezone.localdate()
    if period == "today":
        return today, today
    if period == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if period == "this_week":
        return today - timedelta(days=today.weekday()), today
    if period == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        return this_week_start - timedelta(days=7), this_week_start - timedelta(days=1)
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == "this_year":
        return today.replace(month=1, day=1), today
    return None


def apply_period_filter(qs, period):
    """Restrict ``qs`` to clients whose governing enrollment -- their own or
    their household's -- was OPENED within the period window. No-op when the
    period maps to no range. Caller is responsible for ``.distinct()`` (this
    adds multi-valued joins)."""
    rng = period_date_range(period)
    if not rng:
        return qs
    start, end = rng
    return qs.filter(
        Q(
            enrollments__opened_at__date__gte=start,
            enrollments__opened_at__date__lte=end,
        )
        | Q(
            household_membership__household__enrollment_verifications__opened_at__date__gte=start,
            household_membership__household__enrollment_verifications__opened_at__date__lte=end,
        )
    )


def apply_enrollment_date_filter(qs, field, start, end):
    """Restrict ``qs`` to clients whose governing enrollment -- their own or
    their household's -- has the datetime ``field`` (e.g. ``opened_at`` for when
    the verification was requested, ``verified_at`` for when it was completed)
    within the inclusive [start, end] date window. Either bound may be None
    (open-ended). No-op when both bounds are None. The conditions for a bound
    are ANDed on the SAME joined row (matching ``apply_period_filter``); the
    caller is responsible for ``.distinct()`` (this adds multi-valued joins)."""
    if not start and not end:
        return qs
    hh = "household_membership__household__enrollment_verifications__"
    own_cond, hh_cond = {}, {}
    if start:
        own_cond[f"enrollments__{field}__date__gte"] = start
        hh_cond[f"{hh}{field}__date__gte"] = start
    if end:
        own_cond[f"enrollments__{field}__date__lte"] = end
        hh_cond[f"{hh}{field}__date__lte"] = end
    return qs.filter(Q(**own_cond) | Q(**hh_cond))


def apply_verification_date_filters(qs, params):
    """Apply the Verification-page requested/completed date-range filters from
    query params (``requested_from``/``requested_to`` -> enrollment ``opened_at``;
    ``completed_from``/``completed_to`` -> enrollment ``verified_at``). Returns
    (qs, changed) where ``changed`` signals the caller to ``.distinct()``."""
    changed = False
    req_from, req_to = _parse_date(params.get("requested_from")), _parse_date(params.get("requested_to"))
    if req_from or req_to:
        qs = apply_enrollment_date_filter(qs, "opened_at", req_from, req_to)
        changed = True
    comp_from, comp_to = _parse_date(params.get("completed_from")), _parse_date(params.get("completed_to"))
    if comp_from or comp_to:
        qs = apply_enrollment_date_filter(qs, "verified_at", comp_from, comp_to)
        changed = True
    return qs, changed


class MenuTypesListView(PortalAPIView):
    """Active menu types for the Members-page menu-type filter dropdown.
    ``value`` matches ``MemberDietaryProfile.menu_type`` (the catalog name)."""

    def get(self, request):
        rows = MenuType.objects.filter(is_active=True).order_by("name")
        return Response([{"value": mt.name, "label": mt.name} for mt in rows])


class MembersListView(PortalGenericAPIView):
    serializer_class = s.MemberListSerializer

    def get_queryset(self):
        qs = (
            Client.objects.all()
            .select_related("household_membership__household")
            .prefetch_related(*MEMBER_LIST_PREFETCH)
        )
        params = self.request.query_params

        search = (params.get("search") or "").strip()
        if search:
            cond = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(insurances__external_member_id__icontains=search)
            )
            # Multi-word "first last" search.
            parts = search.split()
            if len(parts) >= 2:
                cond |= Q(first_name__icontains=parts[0]) & Q(
                    last_name__icontains=parts[-1]
                )
            dob = _parse_date(search)
            if dob:
                cond |= Q(date_of_birth=dob)
            try:
                cond |= Q(client_id=uuid.UUID(search))
            except (ValueError, TypeError, AttributeError):
                pass
            qs = qs.filter(cond)

        # Page-level scope (Verification / Logistics) restricts which members are
        # ever shown, before the per-status filter chips are applied.
        scope = (params.get("scope") or "").strip()
        if scope == "verification":
            # Full verification history: the pending queue + anything ever
            # verified, regardless of the stage it later advanced to. Also
            # restrict to members whose household primary holds an Internal
            # Service case (see require_internal_service_primary).
            qs = require_internal_service_primary(qs.filter(verification_scope_q()))
        else:
            scope_stages = SCOPE_TO_STAGES.get(scope)
            if scope_stages:
                qs = qs.filter(lifecycle_stage__in=scope_stages)

        # Logistics (kitchen-assignment) page: drop members whose internal-
        # service (meal/box) cases are ALL closed/cancelled -- they've finished
        # service and shouldn't wait for a kitchen. Kept if ANY internal-service
        # case is still open (blank/unknown status counts as open, so we never
        # over-hide). The case is held by the household primary, so a household
        # drops out once the primary's case is done; dependents follow via the
        # roster build.
        if scope == "logistics":
            open_internal_case = (
                Case.objects.filter(
                    client=OuterRef("pk"),
                    case_type=CaseType.INTERNAL_SERVICE,
                )
                .exclude(
                    case_status__in=(CaseStatus.CLOSED, CaseStatus.CANCELLED)
                )
            )
            qs = qs.filter(Exists(open_internal_case))
            # A paused (On Hold) household keeps lifecycle_stage=kitchen_assignment
            # -- the hold is an overlay on the underlying stage -- so exclude it
            # here to actually remove it from the kitchen-assignment queue (e.g.
            # a sole internal-service case that was denied auto-pauses the member).
            qs = qs.exclude(
                Q(enrollments__stage=EnrollmentStage.ON_HOLD)
                | Q(
                    household_membership__household__enrollment_verifications__stage=(
                        EnrollmentStage.ON_HOLD
                    )
                )
            )

        status_val = (params.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            if status_val in ("Denied", "not_eligible"):
                # Eligibility denial only (lifecycle_stage not_eligible). A denied
                # case AUTHORIZATION is no longer an eligibility/verification
                # state -- it is surfaced via the separate `authorization` filter.
                qs = qs.filter(lifecycle_stage="not_eligible")
            elif status_val in ("verified_awaiting", "Verified"):
                # Verification page "Verified" chip: the pop-up was completed
                # (verified_at set). Independent of the case authorization status.
                qs = qs.filter(verification_completed_q())
            elif status_val in ("pending_verification", "Pending"):
                # Verification page "Pending Verification" chip: pop-up NOT yet
                # completed (verified_at null), regardless of any case auth status.
                qs = qs.exclude(verification_completed_q())
            elif status_val in ("On Hold", "on_hold"):
                # On Hold is a service-state overlay (not a lifecycle_stage), so it
                # is filtered on the member's/household's enrollment stage --
                # independent of verification status or authorization.
                qs = qs.filter(
                    Q(enrollments__stage=EnrollmentStage.ON_HOLD)
                    | Q(
                        household_membership__household__enrollment_verifications__stage=(
                            EnrollmentStage.ON_HOLD
                        )
                    )
                )
            else:
                stages = STATUS_TO_STAGES.get(status_val)
                if stages:
                    qs = qs.filter(lifecycle_stage__in=stages)
                else:
                    qs = qs.filter(lifecycle_stage=status_val)

        # Authorization filter (separate dimension from verification): match the
        # client's GOVERNING internal-service case authorization. Composes with
        # the status chips.
        auth_val = (params.get("authorization") or "").strip().lower()
        if auth_val in AUTHORIZATION_FILTERS:
            qs = apply_authorization_filter(qs, auth_val)

        # Internal-service filter: only members who hold an Internal Service
        # case (the meal/box case the verification + delivery attach to; in our
        # data this is the household primary). Independent of the status chips,
        # so it composes with "All" or any verification status.
        if (params.get("has_internal_service") or "").strip().lower() in (
            "1", "true", "yes",
        ):
            qs = qs.filter(cases__case_type=CaseType.INTERNAL_SERVICE)

        # Product-kind filter (Meals vs Boxes), keyed off the household's program
        # name. A household is always one kind, so meals/boxes never mix.
        service_type = (params.get("service_type") or "").strip().lower()
        kw = {"meals": "meal", "boxes": "box"}.get(service_type)
        if kw:
            qs = qs.filter(
                Q(enrollments__program_name__icontains=kw)
                | Q(
                    household_membership__household__enrollment_verifications__program_name__icontains=kw
                )
            )

        # Kitchen filter: the member's (or their household's) enrollment kitchen.
        kitchen_id = (params.get("kitchen") or "").strip()
        if kitchen_id:
            qs = qs.filter(
                Q(enrollments__kitchen_id=kitchen_id)
                | Q(
                    household_membership__household__enrollment_verifications__kitchen_id=kitchen_id
                )
            )

        # Special status flags that aren't lifecycle stages:
        #   * out_of_orbit -> the member has a MemberDietaryProfile the meal rule
        #     couldn't safely fulfill (status OUT_OF_ORBIT).
        #   * on_hold      -> the member's (or household's) enrollment is paused
        #     (On Hold). NB: lifecycle_stage keeps the held-from stage, so this
        #     must be filtered on the enrollment stage, not lifecycle_stage.
        flag = (params.get("flag") or "").strip().lower()
        if flag == "out_of_orbit":
            qs = qs.filter(member_profiles__status=MemberStatus.OUT_OF_ORBIT)
        elif flag == "on_hold":
            qs = qs.filter(
                Q(enrollments__stage=EnrollmentStage.ON_HOLD)
                | Q(
                    household_membership__household__enrollment_verifications__stage=EnrollmentStage.ON_HOLD
                )
            )
        # TEMP diagnostic flags (to be removed): members missing dietary/logistics
        # data. "no_menu_type" -> no dietary profile carries a menu type at all;
        # "no_kitchen" -> neither the member's nor their household's enrollment has
        # a kitchen assigned.
        elif flag == "no_menu_type":
            qs = qs.exclude(member_profiles__menu_type__gt="")
        elif flag == "no_kitchen":
            qs = qs.exclude(
                Q(enrollments__kitchen_id__isnull=False)
                | Q(
                    household_membership__household__enrollment_verifications__kitchen_id__isnull=False
                )
            )

        # Household-composition filter:
        #   "multi"  -> members whose household has more than one member.
        #   "single" -> members in a solo (one-member) household OR ungrouped
        #               individuals with no household (member count <= 1).
        household_filter = (params.get("household") or "").strip().lower()
        if household_filter in ("multi", "single"):
            qs = qs.annotate(
                _hh_member_count=Count(
                    "household_membership__household__members", distinct=True
                )
            )
            if household_filter == "multi":
                qs = qs.filter(_hh_member_count__gt=1)
            else:  # single
                qs = qs.filter(_hh_member_count__lte=1)

        # Menu-type filter (Members page): the member's assigned catalog menu
        # type. MemberDietaryProfile.menu_type stores the catalog NAME, so match
        # on the name passed from the dropdown.
        menu_type_val = (params.get("menu_type") or "").strip()
        if menu_type_val and menu_type_val.lower() != "all":
            qs = qs.filter(member_profiles__menu_type=menu_type_val)

        # Date-period filter (Verification page dropdown): narrow to households
        # whose enrollment record was OPENED within the selected window.
        qs = apply_period_filter(qs, params.get("period"))

        # Verification page requested/completed date-range filters (from/to on
        # the enrollment's opened_at / verified_at respectively).
        qs, _ = apply_verification_date_filters(qs, params)

        return qs.distinct()

    def _serialize_member(self, client, is_primary, relationship=""):
        data = s.MemberListSerializer(client).data
        data["is_primary"] = is_primary
        data["relationship"] = relationship
        return data

    @staticmethod
    def _hidden_in_logistics(client):
        """Members that shouldn't wait in the kitchen-assignment queue and so are
        dropped from the Logistics roster: out-of-orbit members (the meal rule
        can't safely fulfill them) and members whose internal-service case(s) are
        ALL closed/cancelled (service finished). A dependent with no internal-
        service case of their own is kept -- they ride with the household."""
        if s.member_out_of_orbit(client):
            return True
        internal = [
            c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE
        ]
        if internal and all(
            c.case_status in (CaseStatus.CLOSED, CaseStatus.CANCELLED) for c in internal
        ):
            return True
        return False

    @staticmethod
    def _service_type_for_client(client):
        """Meals/Boxes kind derived from the client's enrollment program name
        (prefetched). Empty when neither keyword is present."""
        for enr in client.enrollments.all():
            kind = product_type_kind_for_name(enr.program_name)
            if kind:
                return kind
        return ""

    def _group_entries(self):
        """Lightweight, ordered list of group identifiers for the filtered set
        WITHOUT serializing anyone. Each entry is
        ``{"type": "household"|"individual", "id", "name"}``. Households are
        de-duplicated and ordered (with individuals) by most-recently-added
        (created_at) so pagination is stable and only the requested page is ever
        built + serialized. A household is included when ANY member matches; its
        full roster is loaded when the page is built."""
        rows = self.get_queryset().values_list(
            "client_id", "household_membership__household_id",
            "first_name", "last_name", "created_at",
        )
        hh_ids, seen_hh, individuals = [], set(), []
        # Most-recent created_at seen among a household's matching members, used
        # as the household's "added" sort timestamp.
        hh_added = {}
        for cid, hid, fn, ln, created in rows:
            if hid:
                if hid not in seen_hh:
                    seen_hh.add(hid)
                    hh_ids.append(hid)
                if created is not None and (
                    hh_added.get(hid) is None or created > hh_added[hid]
                ):
                    hh_added[hid] = created
            else:
                name = f"{(fn or '').strip()} {(ln or '').strip()}".strip()
                individuals.append((cid, name, created))

        # Household sort name = household name, else its primary's name (one query).
        hh_names = {}
        if hh_ids:
            for hid, hname, fn, ln in HouseholdMember.objects.filter(
                household_id__in=hh_ids, is_primary=True
            ).values_list(
                "household_id", "household__name",
                "client__first_name", "client__last_name",
            ):
                hh_names[hid] = (
                    hname or f"{(fn or '').strip()} {(ln or '').strip()}".strip()
                )

        entries = [
            {"type": "household", "id": hid, "name": hh_names.get(hid, ""),
             "added": hh_added.get(hid)}
            for hid in hh_ids
        ] + [
            {"type": "individual", "id": cid, "name": name, "added": created}
            for cid, name, created in individuals
        ]

        # Most recently added (created_at) first; groups with no created_at sort
        # last; name breaks ties (case-insensitive) for stable pagination.
        def _sort_key(e):
            ts = e["added"]
            name = (e["name"] or "").lower()
            if ts is None:
                return (1, 0.0, name)
            return (0, -ts.timestamp(), name)

        entries.sort(key=_sort_key)
        return entries

    def _logistics_kitchens(self):
        """Active-and-inactive kitchens with their offered menus + restrictions
        prefetched, loaded once per request for the serviceability checks
        (serving_kitchens_for_member filters to ACTIVE itself)."""
        return list(
            Kitchen.objects.all().prefetch_related(
                "kitchen_menu_types__menu_type",
                "kitchen_menu_types__restrictions",
            )
        )

    def _logistics_checks(self, primary_client, member_clients, kitchens, *, is_boxes):
        """Compute the Logistics readiness checkers for one household/individual:
        per-member menu type / allergies / predicted Out-of-Orbit, plus the
        household-level delivery address, requested cadence (delivery weekdays),
        and whether ANY single kitchen can serve every eligible member.

        Returns ``(per_member: {client_id_str: {...}}, aggregate: {...})``."""
        enr = s.active_enrollment(primary_client)
        profiles = {}
        if enr is not None:
            for mp in enr.member_profiles.all():
                if mp.client_id:
                    profiles[mp.client_id] = mp
        required = required_product_for_program(enr.program_name) if enr else None

        per_member, serving_sets = {}, []
        missing_menu = predicted_out = 0
        for c in member_clients:
            mp = profiles.get(c.client_id)
            out, reason = predict_member_out_of_orbit(mp)
            menu_type = (mp.menu_type if mp else "") or ""
            if not menu_type:
                missing_menu += 1
            if out:
                predicted_out += 1
            else:
                # Kitchens that can serve this member's menu + allergies for the
                # household's product (meals/boxes). A household is servable only
                # if ONE kitchen serves every eligible member (set intersection).
                serving_sets.append({
                    sk["kitchen"].pk
                    for sk in serving_kitchens_for_member(
                        mp, kitchens=kitchens, required_product=required,
                    )
                })
            per_member[str(c.client_id)] = {
                "menu_type": menu_type,
                "allergies": _allergy_labels(mp.food_allergies if mp else []),
                "predicted_out_of_orbit": out,
                "predicted_reason": reason,
            }

        kitchen_available = bool(set.intersection(*serving_sets)) if serving_sets else False
        address = _format_address(enr.delivery_address) if enr else ""
        weekdays = list(enr.delivery_weekdays or []) if enr else []

        # NB: delivery cadence (weekdays) is CHOSEN in the kitchen-assignment
        # modal, so it is normally unset here -- it's shown as informational
        # ("requested days", if any) and never counts as a readiness blocker.
        blockers = []
        if not address:
            blockers.append("No delivery address")
        if missing_menu:
            blockers.append(f"{missing_menu} missing menu type")
        if predicted_out:
            blockers.append(f"{predicted_out} may get out of orbit")
        if not kitchen_available:
            blockers.append("Kitchen needs review")

        aggregate = {
            "delivery_address": address,
            "delivery_weekdays": weekdays,
            "is_boxes": is_boxes,
            "kitchen_available": kitchen_available,
            "menu_type_missing": missing_menu,
            "predicted_out_of_orbit": predicted_out,
            "ready": not blockers,
            "blockers": blockers,
        }
        return per_member, aggregate

    def _compute_logistics_checks(self, entries):
        """Compute logistics checkers for EVERY entry (used by the readiness
        filter, which must decide before pagination). Returns
        ``{(type, id): (per_member, aggregate)}``."""
        kitchens = self._logistics_kitchens()
        out = {}
        hh_ids = [e["id"] for e in entries if e["type"] == "household"]
        ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
        if hh_ids:
            members = (
                HouseholdMember.objects.filter(household_id__in=hh_ids)
                .select_related("client")
                .prefetch_related("client__enrollments", "client__member_profiles",
                                  "client__cases")
            )
            by_hh = {}
            for hm in members:
                by_hh.setdefault(hm.household_id, []).append(hm)
            for hid in hh_ids:
                hms = [
                    h for h in by_hh.get(hid, [])
                    if h.client and not self._hidden_in_logistics(h.client)
                ]
                if not hms:
                    continue
                primary_hm = next((h for h in hms if h.is_primary), hms[0])
                is_boxes = self._service_type_for_client(primary_hm.client) == "boxes"
                out[("household", hid)] = self._logistics_checks(
                    primary_hm.client, [h.client for h in hms], kitchens, is_boxes=is_boxes,
                )
        if ind_ids:
            clients = Client.objects.filter(client_id__in=ind_ids).prefetch_related(
                "enrollments", "member_profiles", "cases",
            )
            for c in clients:
                if self._hidden_in_logistics(c):
                    continue
                is_boxes = self._service_type_for_client(c) == "boxes"
                out[("individual", c.client_id)] = self._logistics_checks(
                    c, [c], kitchens, is_boxes=is_boxes,
                )
        return out

    def _attach_logistics(self, group, primary_client, member_clients, member_data,
                          kitchens, *, precomputed=None):
        """Attach the logistics checkers to a group: per-member fields onto each
        member dict and the household aggregate as ``group["logistics"]``. Uses
        ``precomputed`` (from the readiness filter pass) when available, else
        computes for this group."""
        if precomputed is not None:
            per_member, aggregate = precomputed
        else:
            per_member, aggregate = self._logistics_checks(
                primary_client, member_clients, kitchens,
                is_boxes=group.get("service_type") == "boxes",
            )
        for md in member_data:
            md.update(per_member.get(md["id"], {}))
        group["logistics"] = aggregate

    def _build_groups_for_page(self, entries, checks=None):
        """Serialize ONLY the groups on the current page, preserving order."""
        hh_ids = [e["id"] for e in entries if e["type"] == "household"]
        ind_ids = [e["id"] for e in entries if e["type"] == "individual"]
        groups_by_key = {}
        # Logistics (kitchen-assignment) hides out-of-orbit / finished-case
        # members from each household roster (see _hidden_in_logistics).
        logistics = (self.request.query_params.get("scope") or "").strip() == "logistics"
        kitchens = self._logistics_kitchens() if logistics else None

        if hh_ids:
            members = (
                HouseholdMember.objects.filter(household_id__in=hh_ids)
                .select_related("household", "client")
                .prefetch_related(
                    "client__insurances", "client__military_profile",
                    Prefetch(
                        "client__enrollments",
                        queryset=EnrollmentVerification.objects.select_related(
                            "requested_by", "verified_by"
                        ),
                    ),
                    "client__member_profiles", "client__cases",
                )
                .order_by("-is_primary", "added_at")
            )
            by_hh = {}
            for hm in members:
                by_hh.setdefault(hm.household_id, []).append(hm)
            for hid in hh_ids:
                hms = by_hh.get(hid)
                if not hms:
                    continue
                if logistics:
                    hms = [
                        h for h in hms
                        if h.client and not self._hidden_in_logistics(h.client)
                    ]
                    if not hms:
                        continue  # whole household hidden -> drop from the page
                primary_hm = next((h for h in hms if h.is_primary), hms[0])
                member_data = [
                    self._serialize_member(h.client, h.is_primary, h.relationship)
                    for h in hms
                ]
                primary_data = next(
                    (m for m in member_data if m["id"] == str(primary_hm.client_id)),
                    member_data[0],
                )
                group = {
                    "id": str(hid),
                    "type": "household",
                    "name": primary_hm.household.name or primary_data["name"],
                    "member_count": len(member_data),
                    "service_type": self._service_type_for_client(primary_hm.client),
                    "primary": primary_data,
                    "members": member_data,
                }
                if logistics:
                    self._attach_logistics(
                        group, primary_hm.client, [h.client for h in hms],
                        member_data, kitchens,
                        precomputed=(checks or {}).get(("household", hid)),
                    )
                groups_by_key[("household", hid)] = group

        if ind_ids:
            clients = Client.objects.filter(client_id__in=ind_ids).prefetch_related(
                *MEMBER_LIST_PREFETCH, "cases"
            )
            for c in clients:
                if logistics and self._hidden_in_logistics(c):
                    continue  # out-of-orbit / finished-case individual
                primary_data = self._serialize_member(c, True)
                group = {
                    "id": str(c.client_id),
                    "type": "individual",
                    "name": primary_data["name"],
                    "member_count": 1,
                    "service_type": self._service_type_for_client(c),
                    "primary": primary_data,
                    "members": [primary_data],
                }
                if logistics:
                    self._attach_logistics(
                        group, c, [c], [primary_data], kitchens,
                        precomputed=(checks or {}).get(("individual", c.client_id)),
                    )
                groups_by_key[("individual", c.client_id)] = group

        # Preserve the paginated order from `entries`.
        return [
            groups_by_key[(e["type"], e["id"])]
            for e in entries
            if (e["type"], e["id"]) in groups_by_key
        ]

    def get(self, request):
        # Flat mode: one row per individual member (no household grouping),
        # used by the Members page. Otherwise return household groups.
        if request.query_params.get("flat"):
            # Order + paginate in SQL (LIMIT/OFFSET) so we only ever serialize
            # one page; serializing/sorting the whole clients table per request
            # does not scale once the full member base is imported. Most recently
            # added members (by created_at) first; rows without a created_at sort
            # last; name (case-insensitive "First Last") breaks ties.
            qs = self.get_queryset().order_by(
                F("created_at").desc(nulls_last=True),
                Lower("first_name"),
                Lower("last_name"),
            )
            page = self.paginate_queryset(qs)
            data = [s.MemberListSerializer(c).data for c in page]
            return self.get_paginated_response(data)
        # Grouped mode (Verification / Logistics): build the ordered group keys
        # cheaply, paginate THEM, and serialize only the current page's groups
        # (previously the whole scoped set was serialized on every request).
        entries = self._group_entries()
        scope = (request.query_params.get("scope") or "").strip()
        checks = None
        # Logistics readiness filter: keep only households that are Ready to
        # assign, or that have blockers. Readiness depends on computed checks
        # (menu type / address / kitchen serviceability), so it must be resolved
        # BEFORE pagination -- compute checks for every entry, then filter.
        readiness = (request.query_params.get("readiness") or "").strip().lower()
        if scope == "logistics" and readiness in ("ready", "blockers"):
            checks = self._compute_logistics_checks(entries)
            want_ready = readiness == "ready"
            entries = [
                e for e in entries
                if bool((checks.get((e["type"], e["id"])) or (None, {"ready": False}))[1]["ready"])
                == want_ready
            ]
        page = self.paginate_queryset(entries)
        return self.get_paginated_response(
            self._build_groups_for_page(page or [], checks=checks)
        )


class MembersStatsView(PortalAPIView):
    def get(self, request):
        qs = Client.objects.all()
        scope = (request.query_params.get("scope") or "").strip()
        # Mirror the Verification list's scope so the chip counts match the rows
        # actually shown: the full verification history (pending + ever-verified)
        # plus the internal-service-primary eligibility filter. The joins make
        # rows non-unique, so all counts below must be DISTINCT on the client.
        if scope == "verification":
            qs = require_internal_service_primary(
                qs.filter(verification_scope_q())
            ).distinct()
        else:
            scope_stages = SCOPE_TO_STAGES.get(scope)
            if scope_stages:
                qs = qs.filter(lifecycle_stage__in=scope_stages)
        # Date-period filter (mirrors the list) so the chip counts match the
        # rows shown for the selected window.
        period = request.query_params.get("period")
        qs = apply_period_filter(qs, period)
        # Requested/completed date-range filters (mirror the list).
        qs, date_filtered = apply_verification_date_filters(qs, request.query_params)
        if period_date_range(period) or date_filtered:
            qs = qs.distinct()
        counts = {"total": qs.count()}
        for label, stages in STATUS_TO_STAGES.items():
            counts[label.lower()] = qs.filter(lifecycle_stage__in=stages).count()
        # Verification work-area chips are split on whether the verification was
        # actually COMPLETED (enrollment stage), not lifecycle_stage -- so a
        # case-auth-driven waiting_authorization counts as Pending, not Verified.
        # Override the lifecycle-based counts above to match the list filters.
        if scope == "verification":
            completed_q = verification_completed_q()
            counts["verified_awaiting"] = qs.filter(completed_q).distinct().count()
            counts["pending_verification"] = qs.exclude(completed_q).distinct().count()
        # Authorization counts (separate dimension): how many members' GOVERNING
        # internal-service case is in each authorization status. Powers the
        # Authorization filter chips; uses the same governing-aware filter as the
        # list so counts match the rows.
        counts["authorization"] = {
            key: apply_authorization_filter(qs, key).distinct().count()
            for key in AUTHORIZATION_FILTERS
        }
        # Raw per-stage counts (powers stage-specific filter chips on the
        # Verification page).
        counts["stages"] = {
            row["lifecycle_stage"]: row["n"]
            for row in qs.values("lifecycle_stage").annotate(
                n=Count("id", distinct=True)
            )
        }
        return Response(counts)


def _get_member(client_id):
    return get_object_or_404(
        Client.objects.prefetch_related(
            "insurances", "military_profile", "addresses", "tickets__type",
            "enrollments", "cases",
        ),
        pk=client_id,
    )


class MemberDetailView(PortalAPIView):
    def get(self, request, client_id):
        client = _get_member(client_id)
        return Response(s.MemberDetailSerializer(client).data)


class MemberInsuranceView(PortalAPIView):
    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        plans = client.insurances.all()
        return Response(s.PortalInsuranceSerializer(plans, many=True).data)


class MemberSocialCoverageView(PortalAPIView):
    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        plans = client.social_care_coverages.all()
        return Response(s.PortalSocialCoverageSerializer(plans, many=True).data)


class MemberPhonesView(PortalAPIView):
    """GET/POST /members/<client_id>/phones/ — list and add the client's phone
    numbers (the Communication Preferences card on the member profile). Shares
    the ClientPhone model + response shape with the extension caller-ID flow."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        phones = ClientPhone.objects.filter(client_id=client_id)
        return Response([_phone_dict(p) for p in phones])

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        number = (request.data.get("number") or "").strip()
        normalized = ClientPhone.normalize(number)
        if not normalized:
            return Response(
                {"error": "A valid phone number (at least 10 digits) is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        label = (request.data.get("label") or "").strip()
        # Idempotent on (client, normalized): re-adding the same number refreshes
        # it rather than tripping the unique constraint.
        phone, created = ClientPhone.objects.get_or_create(
            client=client,
            normalized=normalized,
            defaults={"raw": number, "label": label, "source": ClientPhoneSource.AGENT},
        )
        phone.last_seen_at = timezone.now()
        if not created and label and not phone.label:
            phone.label = label
        phone.save(update_fields=["last_seen_at", "label"])
        # First number a client ever gets becomes primary by default.
        make_primary = bool(request.data.get("is_primary")) or (
            created and not ClientPhone.objects.filter(
                client=client, is_primary=True
            ).exclude(pk=phone.pk).exists()
        )
        if make_primary:
            ClientPhone.objects.filter(client=client, is_primary=True).exclude(
                pk=phone.pk
            ).update(is_primary=False)
            phone.is_primary = True
            phone.save(update_fields=["is_primary"])
        return Response(
            _phone_dict(phone),
            status=http.HTTP_201_CREATED if created else http.HTTP_200_OK,
        )


class MemberPhoneDetailView(PortalAPIView):
    """PATCH/DELETE /members/<client_id>/phones/<client_phone_id>/ — edit the
    label / primary flag, or remove a number."""

    def patch(self, request, client_id, client_phone_id):
        phone = get_object_or_404(
            ClientPhone, pk=client_phone_id, client_id=client_id
        )
        if bool(request.data.get("is_primary")):
            ClientPhone.objects.filter(
                client_id=client_id, is_primary=True
            ).exclude(pk=phone.pk).update(is_primary=False)
            phone.is_primary = True
            phone.save(update_fields=["is_primary"])
        if "label" in request.data:
            phone.label = (request.data.get("label") or "").strip()
            phone.save(update_fields=["label"])
        return Response(_phone_dict(phone))

    def delete(self, request, client_id, client_phone_id):
        phone = get_object_or_404(
            ClientPhone, pk=client_phone_id, client_id=client_id
        )
        phone.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class MemberHistoryView(PortalGenericAPIView):
    serializer_class = s.HistoryEventSummarySerializer

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = TimelineEvent.objects.filter(client_id=client_id)
        page = self.paginate_queryset(qs)
        data = self.get_serializer(page, many=True).data
        return self.get_paginated_response(data)


class MemberHistoryDetailView(PortalAPIView):
    def get(self, request, client_id, event_id):
        event = get_object_or_404(
            TimelineEvent, pk=event_id, client_id=client_id
        )
        return Response(s.HistoryEventDetailSerializer(event).data)


class MemberOrdersView(PortalGenericAPIView):
    """Purchase orders that include a delivery for this member."""

    serializer_class = s.PortalMemberOrderSerializer

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = (
            PurchaseOrder.objects.filter(delivery_orders__member_id=client_id)
            .distinct()
            .prefetch_related("delivery_orders", "kitchen", "delivery_company")
        )
        status_val = (request.query_params.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            qs = qs.filter(status=status_val)
        page = self.paginate_queryset(qs)
        data = self.get_serializer(
            page, many=True, context={"member_id": str(client_id)}
        ).data
        return self.get_paginated_response(data)


class MemberHouseholdView(PortalAPIView):
    """Household tab: address + per-member dietary, from the active enrollment."""

    def _enrollment(self, client_id):
        client = get_object_or_404(Client, pk=client_id)
        return s.active_enrollment(client)

    def get(self, request, client_id):
        enr = self._enrollment(client_id)
        if enr is None:
            return Response({"enrollment": None, "address": None, "members": []})
        # Heal any drift between the household roster and this enrollment's
        # per-member profiles, so members tied via the extension picker (which
        # only writes a HouseholdMember row) appear here with dietary/menu/status
        # and share the enrollment's address/service. Idempotent.
        sync_household_members(enr.client, enrollment=enr)
        members = enr.member_profiles.select_related(
            "client__household_membership"
        ).all()
        addr = enr.delivery_address
        program_name = (
            (enr.case.program.name if enr.case and enr.case.program_id else "")
            or enr.program_name
        )
        kind = product_type_kind_for_name(program_name)
        cadence = current_household_cadence(enr)
        return Response(
            {
                "enrollment": {
                    "id": enr.pk, "code": enr.code, "stage": enr.stage,
                    "kitchen_id": str(enr.kitchen_id) if enr.kitchen_id else None,
                    "kitchen_name": enr.kitchen.name if enr.kitchen_id else "",
                    "service_type": kind.value if kind else "",
                    "service_type_label": kind.label if kind else "",
                    "cadence": cadence,
                    "cadence_label": dict(DeliveryCadence.choices).get(cadence, ""),
                    "cadence_options": cadence_options_for_kind(kind),
                },
                "address": {
                    "street": addr.street, "unit": addr.unit, "city": addr.city,
                    "state": addr.state, "zip": addr.zip,
                    "notes": addr.notes,
                }
                if addr
                else None,
                "members": s.PortalHouseholdMemberSerializer(members, many=True).data,
            }
        )

    def patch(self, request, client_id):
        # Edit the household delivery address.
        enr = self._enrollment(client_id)
        if enr is None:
            return Response(
                {"error": "No active enrollment for this member."},
                status=http.HTTP_404_NOT_FOUND,
            )
        ser = s.PortalAddressEditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        addr = enr.delivery_address
        previous = timeline._format_address(addr) if addr is not None else ""
        if addr is None:
            addr = Address.objects.create(client_id=client_id, type="temporary")
            enr.delivery_address = addr
            enr.save(update_fields=["delivery_address"])
        for field in ("street", "unit", "city", "state", "zip", "notes"):
            if field in data:
                setattr(addr, field, data[field])
        addr.save()
        new_addr = timeline._format_address(addr)
        if new_addr != previous:
            agent = current_agent(request)
            try:
                timeline.event_for_delivery_address_change(
                    enr.client, addr, previous=previous, enrollment=enr,
                    actor=(f"agent:{agent.code}" if agent and agent.code else ""),
                )
            except Exception:  # never let history-logging break the edit
                pass
        return Response(
            {"street": addr.street, "unit": addr.unit, "city": addr.city,
             "state": addr.state, "zip": addr.zip, "notes": addr.notes}
        )


class MemberHouseholdSearchView(PortalAPIView):
    """Search existing clients (by client ID or Medicaid/insurance member ID) to
    add to this member's household. Mirrors the extension's client picker."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        return Response(search_clients(request.query_params.get("q")))


class MemberHouseholdAddView(PortalAPIView):
    """Add an existing client to this member's household. Moves the client out of
    any other household first (one-household-per-client). No family-size cap on
    the CRM -- agents are authoritative."""

    def post(self, request, client_id):
        primary = get_object_or_404(Client, pk=client_id)
        member_id = request.data.get("client_id")
        if not member_id:
            return Response(
                {"error": "client_id is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        try:
            member_client = Client.objects.get(pk=member_id)
        except (Client.DoesNotExist, ValueError):
            return Response(
                {"error": "Client not found."}, status=http.HTTP_404_NOT_FOUND
            )
        if str(member_client.pk) == str(primary.pk):
            return Response(
                {"error": "A client can't be added to their own household."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        add_client_to_household(primary, member_client)
        agent = current_agent(request)
        actor = f"agent:{agent.agent_code}" if agent and agent.agent_code else ""
        try:
            timeline.event_for_household_member_added(
                primary, member_client,
                enrollment=s.active_enrollment(primary), actor=actor,
            )
        except Exception:  # never let history-logging break the add
            pass
        return Response({"client_id": str(member_client.pk)}, status=http.HTTP_201_CREATED)


class MemberInternalCaseDescriptionsView(PortalAPIView):
    """TEMPORARY: the case descriptions of this client's INTERNAL_SERVICE cases,
    surfaced on the Household tab. Read-only. Slated for removal in a few days."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        cases = (
            Case.objects.filter(
                client_id=client_id, case_type=CaseType.INTERNAL_SERVICE,
            )
            .exclude(case_description="")
            .order_by("-date_opened")
        )
        return Response([
            {
                "case_id": str(c.pk),
                "program_name": c.program_name or c.service_type or "",
                "status": c.get_case_status_display() if c.case_status else "",
                "description": c.case_description,
            }
            for c in cases
        ])


class HouseholdMemberEditView(PortalAPIView):
    """PATCH a single household member's dietary info (MemberDietaryProfile)."""

    def patch(self, request, client_id, member_id):
        # Scope the profile to the URL client's ACTIVE enrollment -- the exact
        # set the Household tab (HouseholdView.get) lists. A non-primary member
        # has no enrollment of their own, so active_enrollment() falls back to
        # the household's enrollment (owned by the primary). Filtering by
        # ``enrollment__client_id=client_id`` would 404 for such members, since
        # the enrollment's owner is the primary, not the member being viewed.
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        mv = get_object_or_404(
            MemberDietaryProfile, pk=member_id, enrollment=enr,
        )
        ser = s.PortalMemberDietaryEditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        # `reactivate` / `deactivate` are control flags, not model fields —
        # handle them separately from the assignable dietary fields.
        reactivate = data.pop("reactivate", False)
        deactivate = data.pop("deactivate", False)
        for field, value in data.items():
            setattr(mv, field, value)

        if deactivate and mv.status == MemberStatus.ACTIVE:
            # Manual agent override: pull the member Out of Orbit regardless of
            # the meal rule. Clear the kitchen meal result so they're excluded
            # from every delivery schedule / Purchase Order until reactivated.
            mv.status = MemberStatus.OUT_OF_ORBIT
            mv.kitchen_meal_type = ""
            mv.kitchen_food_notes = ""
            mv.save()
            agent = current_agent(request)
            actor = f"agent:{agent.agent_code}" if agent and agent.agent_code else ""
            try:
                timeline.event_for_out_of_orbit(
                    mv, enrollment=mv.enrollment,
                    reason="Manually set out of orbit by agent.", actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
            # Leave a system note (same as the auto-out-of-orbit paths).
            if mv.client_id:
                try:
                    Note.objects.create(
                        client=mv.client, source=NoteSource.SYSTEM,
                        body=NO_KITCHEN_OUT_OF_ORBIT_NOTE,
                    )
                except Exception:  # never let note-writing break the edit
                    pass
        elif reactivate and mv.status == MemberStatus.OUT_OF_ORBIT:
            # Re-run the kitchen-aware rules against the edited menu type /
            # allergies. Only return the member to Active if the new combination
            # can actually be fulfilled by the household's assigned kitchen;
            # otherwise the agent must pick a different menu type.
            out, _became, reason = reconcile_member_kitchen_output(
                mv, enr.kitchen, save=False,
            )
            if out:
                return Response(
                    {"error": reason or "This menu type can't be fulfilled for this member."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            mv.save()
            agent = current_agent(request)
            actor = f"agent:{agent.agent_code}" if agent and agent.agent_code else ""
            try:
                timeline.event_for_member_reactivated(
                    mv, enrollment=mv.enrollment, actor=actor,
                )
            except Exception:  # never let history-logging break the edit
                pass
        else:
            # Normal save: reconcile the kitchen output against the global meal
            # rules AND the household's assigned kitchen. An ACTIVE member whose
            # new menu/allergies can't be fulfilled is auto-set Out of Orbit
            # (excluded from schedules/POs). Out-of-orbit members are NOT
            # auto-reactivated here -- that requires the explicit reactivate flag
            # above, so a manual override is never silently undone by an edit.
            became_out = False
            if mv.status == MemberStatus.ACTIVE:
                _out, became_out, reason = reconcile_member_kitchen_output(
                    mv, enr.kitchen, save=False,
                )
            mv.save()
            if became_out:
                agent = current_agent(request)
                actor = f"agent:{agent.agent_code}" if agent and agent.agent_code else ""
                try:
                    timeline.event_for_out_of_orbit(
                        mv, enrollment=mv.enrollment,
                        reason=reason or "Menu/allergies can't be fulfilled by the assigned kitchen.",
                        actor=actor,
                    )
                except Exception:  # never let history-logging break the edit
                    pass
                # Leave a customer-facing note explaining why the edit pulled the
                # member Out of Orbit.
                if mv.client_id:
                    try:
                        Note.objects.create(
                            client=mv.client, source=NoteSource.SYSTEM,
                            body=NO_KITCHEN_OUT_OF_ORBIT_NOTE,
                        )
                    except Exception:  # never let note-writing break the edit
                        pass

        # Propagate the edited menu type / allergies onto this member's future
        # SCHEDULED delivery occurrences so PO generation reflects the change
        # (those rows snapshot the profile at calendar-build time).
        resync_scheduled_orders(enrollment=mv.enrollment)

        return Response(s.PortalHouseholdMemberSerializer(mv).data)


class MemberServiceHoldView(PortalAPIView):
    """Pause the member's household service.

    Moves the active enrollment to On Hold (which logs a StageEvent and mirrors
    a 'Stage changed to On Hold' entry onto the timeline), then records a client
    note with the reason. While On Hold the household is excluded from any new
    Purchase Order until service is resumed.
    """

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        if enr is None:
            return Response(
                {"error": "This member has no active enrollment to place on hold."},
                status=http.HTTP_404_NOT_FOUND,
            )
        if EnrollmentStage(enr.stage) == EnrollmentStage.ON_HOLD:
            return Response(
                {"error": "Service is already on hold."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response(
                {"reason": "A reason is required to place service on hold."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        agent = current_agent(request)
        author = agent.name if agent else ""
        try:
            advance_enrollment(
                enr, EnrollmentStage.ON_HOLD,
                note=f"Placed on hold by {author or 'support portal'}. Reason: {reason}",
            )
        except InvalidTransition as exc:
            return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        Note.objects.create(
            client=client, source=NoteSource.AGENT, author_name=author,
            body=f"Service placed on hold. Reason: {reason}",
        )
        return Response(s.MemberDetailSerializer(client).data)


class MemberServiceResumeView(PortalAPIView):
    """Resume a held household.

    Returns the enrollment to the stage it was in before the hold (defaulting to
    Service Active), which logs a StageEvent + timeline entry and re-includes the
    household in Purchase Order batching. Records a client note.
    """

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        enr = s.active_enrollment(client)
        if enr is None or EnrollmentStage(enr.stage) != EnrollmentStage.ON_HOLD:
            return Response(
                {"error": "Service is not on hold."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        # Resume to the stage the enrollment held from (most recent hold event).
        last_hold = StageEvent.objects.filter(
            enrollment=enr, to_stage=EnrollmentStage.ON_HOLD
        ).first()
        target = EnrollmentStage.SERVICE_ACTIVE
        if last_hold and last_hold.from_stage:
            try:
                target = EnrollmentStage(last_hold.from_stage)
            except ValueError:
                target = EnrollmentStage.SERVICE_ACTIVE
        reason = (request.data.get("reason") or "").strip()
        agent = current_agent(request)
        author = agent.name if agent else ""
        suffix = f" Reason: {reason}" if reason else ""
        try:
            # force=True: a prior process gate (e.g. verification) already passed
            # before the hold, so restoring the prior stage must not be re-gated.
            advance_enrollment(
                enr, target, force=True,
                note=f"Service resumed by {author or 'support portal'}.{suffix}",
            )
        except InvalidTransition as exc:
            return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        Note.objects.create(
            client=client, source=NoteSource.AGENT, author_name=author,
            body=f"Service resumed.{suffix}",
        )
        return Response(s.MemberDetailSerializer(client).data)


class MemberNotesView(PortalGenericAPIView):
    serializer_class = s.PortalNoteSerializer

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        type_filter = (request.query_params.get("type") or "all").lower()
        qs = Note.objects.filter(Q(client_id=client_id) | Q(case__client_id=client_id))
        if type_filter == "client":
            qs = qs.filter(case__isnull=True)
        elif type_filter == "case":
            qs = qs.filter(case__isnull=False)
        qs = qs.distinct()
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        ser = s.PortalNoteCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        agent = current_agent(request)
        note = Note.objects.create(
            client=client,
            case_id=ser.validated_data.get("case_id"),
            source=NoteSource.AGENT,
            author_name=agent.name if agent else "",
            body=ser.validated_data["body"],
        )
        return Response(s.PortalNoteSerializer(note).data, status=http.HTTP_201_CREATED)


class MemberCasesView(PortalAPIView):
    """A member's cases. The lightweight shape powers the New-Ticket “related
    case” dropdown; ``?detail=1`` returns the full shape for the profile's
    Cases tab (authorization, dates, provider, outcome)."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        cases = Case.objects.filter(client_id=client_id).order_by("-date_opened")
        if request.query_params.get("detail"):
            return Response(s.PortalMemberCaseSerializer(cases, many=True).data)
        return Response(s.PortalCaseOptionSerializer(cases, many=True).data)


class MemberCaseHistoryView(PortalGenericAPIView):
    """The client timeline scoped to a single case -- the 'Case history' shown on
    the profile's Cases tab. Same event rows as the client history (with the same
    provenance + deep-links), filtered to this case, newest-first + paginated."""

    serializer_class = s.HistoryEventSummarySerializer

    def get(self, request, client_id, case_id):
        get_object_or_404(Client, pk=client_id)
        get_object_or_404(Case, pk=case_id, client_id=client_id)
        qs = TimelineEvent.objects.filter(client_id=client_id, case_id=case_id)
        page = self.paginate_queryset(qs)
        data = self.get_serializer(page, many=True).data
        return self.get_paginated_response(data)


# Noisy / internal fields to hide from the raw field-diff drill-down.
_AUDIT_EXCLUDE = frozenset({
    "updated_at", "created_at", "crm_sync_hash", "crm_synced_at",
})


def _audit_val(v):
    return "" if v is None else str(v)


class MemberCaseAuditView(PortalAPIView):
    """Raw field-level change history for a case, from django-simple-history --
    the 'forensic' drill-down behind the curated Case history. Each entry lists
    the fields that changed (old -> new) with who/where (change_source/actor)."""

    def get(self, request, client_id, case_id):
        get_object_or_404(Client, pk=client_id)
        case = get_object_or_404(Case, pk=case_id, client_id=client_id)
        records = list(case.history.all())  # newest first
        out = []
        for i, rec in enumerate(records):
            prev = records[i + 1] if i + 1 < len(records) else None
            entry = {
                "changed_at": rec.history_date,
                "source": rec.change_source or "",
                "actor": rec.change_actor or "",
                "type": rec.get_history_type_display(),
                "changes": [],
            }
            if prev is not None:
                try:
                    delta = rec.diff_against(prev, excluded_fields=_AUDIT_EXCLUDE)
                except Exception:  # noqa: BLE001 - never fail the audit view
                    delta = None
                if delta is not None:
                    entry["changes"] = [
                        {
                            "field": c.field,
                            "old": _audit_val(c.old),
                            "new": _audit_val(c.new),
                        }
                        for c in delta.changes
                    ]
                    if not entry["changes"]:
                        continue  # unchanged snapshot -> skip
            out.append(entry)
        return Response({"results": out})


class MemberTicketsView(PortalAPIView):
    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = (
            Ticket.objects.filter(client_id=client_id)
            .select_related("assigned_to", "client", "case", "type")
            .prefetch_related("notes")
        )
        # ?mine=true -> only tickets assigned to the requesting agent.
        mine = (request.query_params.get("mine") or "").strip().lower()
        if mine in ("1", "true", "yes"):
            agent = current_agent(request)
            qs = qs.filter(assigned_to=agent) if agent else qs.none()
        return Response(s.PortalTicketSerializer(qs, many=True).data)


class MemberVerificationCreateView(PortalAPIView):
    """POST: create an EnrollmentVerification + MemberDietaryProfiles + delivery
    Address for a member (the 5-step wizard).

    On save the household is verified: ``verified_at``/``verified_by`` are set
    (the source-of-truth verification fact) and the enrollment advances to
    VERIFIED (driving the client to the "Verified" lifecycle stage). When the
    governing case authorization is "Accepted" the enrollment is advanced to
    KITCHEN_ASSIGNMENT (awaiting the manual kitchen-assignment step, which builds
    the delivery schedule). Each transition is recorded on the client's history
    (StageEvent + timeline event).
    """

    @transaction.atomic
    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        ser = s.VerificationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        # Delivery address (shared by the household). Unit/apt is stored in its
        # own field so the kitchen + delivery label can show it distinctly.
        address = Address.objects.create(
            client=client,
            type="temporary",
            street=data.get("street", ""),
            unit=data.get("apt", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            zip=data.get("zip", ""),
            notes=data.get("address_notes", ""),
        )

        household = getattr(
            getattr(client, "household_membership", None), "household", None
        )
        # Members the agent added via the Step-1 search carry a client_id that
        # isn't the primary. If any are present we need a household to attach
        # them to, so create one (with the primary) when the client has none.
        extra_member_ids = [
            str(m["client_id"])
            for m in data["members"]
            if m.get("client_id") and str(m["client_id"]) != str(client.pk)
        ]
        if extra_member_ids and household is None:
            household = ensure_household_with_primary(client)
        # Start at PENDING_VERIFICATION; the guarded lifecycle transitions below
        # move it forward and write the history rows. The agent running the
        # wizard both requests and (below) completes the verification.
        acting_agent = current_agent(request)
        enrollment = EnrollmentVerification.objects.create(
            client=client,
            household=household,
            program_name=data.get("program_name", ""),
            delivery_address=address,
            delivery_weekdays=data.get("delivery_weekdays", []),
            household_size=len(data["members"]),
            is_family_verified=data.get("is_family_verified"),
            medicaid_type_verified=data.get("medicaid_type_verified"),
            delivery_address_verified=data.get("delivery_address_verified"),
            stage=EnrollmentStage.PENDING_VERIFICATION,
            requested_by=acting_agent,
        )

        for m in data["members"]:
            MemberDietaryProfile.objects.create(
                enrollment=enrollment,
                client_id=m.get("client_id"),
                member_name=m.get("member_name", ""),
                dietary_restrictions=m.get("dietary_restrictions", []),
                food_allergies=m.get("food_allergies", []),
                other_dietary_restrictions=m.get("other_dietary_restrictions", ""),
                meal_category=m.get("meal_category", ""),
                # Menu type is derived from the member's dietary data (allergy
                # overrides win, else meal_category) when not explicitly sent.
                menu_type=m.get("menu_type")
                or menu_type_for_member(
                    food_allergies=m.get("food_allergies", []),
                    meal_category=m.get("meal_category", ""),
                ),
                general_verification_notes=m.get("notes", ""),
            )

            # Wire the member's mobile-app login number onto their HouseholdMember
            # row (the field powers the Benefully member app login). Only members
            # that map to a real client/household-member can be wired here.
            mobile = (m.get("mobile_number") or "").strip()
            member_client_id = m.get("client_id")
            if mobile and member_client_id:
                HouseholdMember.objects.filter(
                    client_id=member_client_id
                ).update(mobile_app_username=mobile)

        # Attach any members added via the Step-1 search to the household and
        # record each addition on the primary's timeline. Skip clients already
        # in another household (one-household-per-client) and existing members
        # of THIS household (no duplicate row, no duplicate timeline event).
        if household is not None and extra_member_ids:
            agent = current_agent(request)
            actor = (
                f"agent:{agent.agent_code}" if agent and agent.agent_code else ""
            )
            for mid in extra_member_ids:
                member_client = Client.objects.filter(pk=mid).first()
                if member_client is None:
                    continue
                membership = HouseholdMember.objects.filter(
                    client=member_client
                ).first()
                if membership is not None:
                    continue  # already in a household (this or another) — leave it
                HouseholdMember.objects.create(
                    household=household, client=member_client, is_primary=False
                )
                try:
                    timeline.event_for_household_member_added(
                        client, member_client, enrollment=enrollment, actor=actor
                    )
                except Exception:  # never let history-logging break the save
                    pass

        # Completing the wizard IS the verification: stamp the source-of-truth
        # fact (verified_at/verified_by), then force past the process gate. This
        # records a StageEvent + timeline event and recomputes the client's
        # lifecycle stage to "Verified".
        agent = current_agent(request)
        enrollment.verified_at = timezone.now()
        enrollment.verified_by = agent
        enrollment.save(update_fields=["verified_at", "verified_by"])
        # Record WHO verified on the history timeline + StageEvent audit. The
        # portal actor is an Agent (not a User), so pass it as a display label.
        actor_label = (
            agent.name or (f"agent:{agent.agent_code}" if agent.agent_code else "")
        ) if agent else ""
        advance_enrollment(
            enrollment, EnrollmentStage.VERIFIED, force=True,
            actor_label=actor_label,
            note="Verification completed via support portal.",
        )

        # The authorization outcome is sourced from the client's case (NOT the
        # client/frontend): only an Accepted (APPROVED) case advances the
        # household to "Kitchen Assignment". Any other status leaves the client
        # at "Verified". The member is NOT auto-activated and no delivery
        # schedule/orders are generated here — that happens later when the
        # kitchen assignment is executed manually (separate page), which is what
        # moves the household into service.
        case = s.primary_case(client)
        accepted = bool(
            case and case.service_authorization_status == ServiceAuthorizationStatus.APPROVED
        )
        if accepted:
            advance_enrollment(
                enrollment, EnrollmentStage.KITCHEN_ASSIGNMENT, force=True,
                actor_label=actor_label,
                note="Authorization accepted — awaiting kitchen assignment.",
            )
            # Best-effort link the case for reporting, but only when it isn't
            # already owned by another enrollment (a case maps to at most one
            # enrollment — uniq_enrollment_verification_per_case).
            if (
                case is not None
                and enrollment.case_id is None
                and not EnrollmentVerification.objects.filter(case=case)
                .exclude(pk=enrollment.pk)
                .exists()
            ):
                enrollment.case = case
                enrollment.save(update_fields=["case"])

        return Response(
            {"id": enrollment.pk, "code": enrollment.code, "stage": enrollment.stage},
            status=http.HTTP_201_CREATED,
        )


def _logistics_enrollment(client_id):
    """The active enrollment for a member, or (None, error_response)."""
    client = get_object_or_404(Client, pk=client_id)
    enr = s.active_enrollment(client)
    if enr is None:
        return None, None, Response(
            {"error": "No active enrollment for this member."},
            status=http.HTTP_404_NOT_FOUND,
        )
    return client, enr, None


def assign_kitchen_to_household(
    enr, client, kitchen, *, cadence, once_weekday=None,
    member_quantities=None, exclude_notes=None, agent=None,
):
    """Assign ``kitchen`` + ``cadence`` to a whole household, apply the kitchen
    output rules to every member, build the delivery plan + calendar, and
    activate service (Service Active).

    Shared by the single-household Logistics assignment and the bulk boxes
    assignment so the meal/kitchen output rules are applied identically. The
    caller must have validated ``kitchen`` and ``cadence`` first.

    ``exclude_notes`` maps a MemberDietaryProfile pk -> a customer-facing note
    for members the agent manually pulled Out of Orbit (the override wins over
    the meal rule). Returns a summary dict for reporting.
    """
    member_quantities = member_quantities or {}
    exclude_notes = exclude_notes or {}
    actor = f"agent:{agent.agent_code}" if agent and agent.agent_code else ""

    enr.kitchen = kitchen
    enr.save(update_fields=["kitchen"])

    # Apply the Meal Rules to each member: derive the kitchen meal type + food
    # notes (sent to the kitchen on the PO) or flag the member Out of Orbit.
    # Reconciliation is kitchen-aware, so a member the CHOSEN kitchen can't
    # fulfill (menu not offered / allergy it can't handle) is also set Out of
    # Orbit. Out-of-orbit members are excluded from schedules + POs.
    offered = kitchen_offered_menu_index(kitchen)
    out_of_orbit = 0
    for profile in enr.member_profiles.select_related("client").all():
        if profile.pk in exclude_notes:
            # Manual exclusion: force Out of Orbit and drop the kitchen meal
            # result so they're excluded from schedules + POs, regardless of
            # what the meal rule would decide.
            note = exclude_notes[profile.pk]
            profile.status = MemberStatus.OUT_OF_ORBIT
            profile.kitchen_meal_type = ""
            profile.kitchen_food_notes = ""
            profile.save(update_fields=[
                "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
            ])
            out_of_orbit += 1
            reason = note or "Excluded from kitchen assignment by agent."
            try:
                timeline.event_for_out_of_orbit(
                    profile, enrollment=enr, reason=reason, actor=actor,
                )
            except Exception:  # never let history-logging break assignment
                pass
            # Add a customer-facing note on the member's own client record.
            if note and profile.client_id:
                try:
                    Note.objects.create(
                        client=profile.client, source=NoteSource.AGENT,
                        author_name=agent.name if agent else "", body=note,
                    )
                except Exception:  # never let note-writing break assignment
                    pass
            continue
        _out, became_out, reason = reconcile_member_kitchen_output(
            profile, kitchen, offered=offered,
        )
        if profile.status == MemberStatus.OUT_OF_ORBIT:
            out_of_orbit += 1
        if became_out:
            try:
                timeline.event_for_out_of_orbit(
                    profile, enrollment=enr,
                    reason=reason or "Allergy/menu combination cannot be safely fulfilled.",
                    actor=actor,
                )
            except Exception:  # never let history-logging break assignment
                pass
            # Note explaining why the assigned kitchen couldn't serve them.
            if profile.client_id:
                try:
                    Note.objects.create(
                        client=profile.client, source=NoteSource.SYSTEM,
                        body=NO_KITCHEN_OUT_OF_ORBIT_NOTE,
                    )
                except Exception:  # never let note-writing break assignment
                    pass

    case = enr.case or s.primary_case(client)
    create_member_delivery_schedules(
        enr, case=case, cadence=cadence, once_a_week_weekday=once_weekday,
        kitchen=kitchen, member_quantities=member_quantities,
    )

    # Expand the per-member plans into the dated delivery calendar
    # (OrderSchedule) so the household shows up for PO generation.
    generate_delivery_calendar(enr)

    # Re-assignment case: when the household ALREADY had a plan + calendar, the
    # two builders above are idempotent no-ops. Push the newly chosen kitchen +
    # refreshed meal-rule results onto the existing plans and future
    # occurrences so PO generation reflects the change.
    enr.delivery_schedules.update(kitchen=kitchen)
    resync_scheduled_orders(enrollment=enr)

    advance_enrollment(
        enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
        note=f"Kitchen assigned ({kitchen.name}); service activated.",
    )
    return {"out_of_orbit": out_of_orbit}


class MemberKitchenOptionsView(PortalAPIView):
    """Logistics: the household's members (read-only dietary), the available
    kitchens with per-member coverage warnings, cadence options and the
    authorization window — everything needed to assign a kitchen."""

    def get(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        data = kitchen_options(enr)
        case = enr.case or s.primary_case(client)
        window = {"starts_on": None, "ends_on": None}
        if case is not None:
            starts = case.service_authorization_approval_starts_at
            ends = case.service_authorization_approval_ends_at
            window = {
                "starts_on": starts.date().isoformat() if starts else None,
                "ends_on": ends.date().isoformat() if ends else None,
            }
        data["enrollment"] = {
            "id": enr.pk,
            "code": enr.code,
            "stage": enr.stage,
            "program_name": enr.program_name,
            "kitchen_id": str(enr.kitchen_id) if enr.kitchen_id else None,
        }
        data["cadence_options"] = cadence_options_for_kind(data.get("product_kind"))
        data["window"] = window
        return Response(data)


class MemberAssignKitchenView(PortalAPIView):
    """Logistics: assign a kitchen + cadence to the whole household, build the
    per-member delivery plans, and activate the household (Service Active).

    PO generation stays a separate manual step. Body:
    ``{kitchen_id, cadence, once_a_week_weekday?, member_quantities?}``.
    """

    @transaction.atomic
    def post(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err

        kitchen_id = request.data.get("kitchen_id")
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None

        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        if kitchen is None:
            return Response(
                {"error": "kitchen_id is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if cadence not in DeliveryCadence.values:
            return Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence == DeliveryCadence.ONCE_A_WEEK and not once_weekday:
            return Response(
                {"error": "once_a_week_weekday is required for a weekly cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # Per-member quantity overrides: {member_profile_id: qty}.
        raw_qty = request.data.get("member_quantities") or {}
        member_quantities = {}
        for key, val in raw_qty.items():
            try:
                member_quantities[int(key)] = int(val)
            except (TypeError, ValueError):
                continue

        # Members the agent manually excluded from THIS assignment (pulled Out of
        # Orbit), each with an optional customer-facing note. Applied AFTER the
        # meal rule so the override wins even when the member could otherwise be
        # served. Body: ``member_overrides: [{member_id, out_of_orbit, note?}]``.
        exclude_notes = {}
        for ov in request.data.get("member_overrides") or []:
            try:
                if ov.get("out_of_orbit"):
                    exclude_notes[int(ov.get("member_id"))] = (ov.get("note") or "").strip()
            except (TypeError, ValueError, AttributeError):
                continue

        assign_kitchen_to_household(
            enr, client, kitchen, cadence=cadence, once_weekday=once_weekday,
            member_quantities=member_quantities, exclude_notes=exclude_notes,
            agent=current_agent(request),
        )
        return Response({
            "id": enr.pk,
            "stage": enr.stage,
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
        })


def _enrollment_kind(enr):
    """Meals/Boxes kind for an enrollment, preferring the case's program name
    (the source of truth) and falling back to the enrollment's own name."""
    program_name = (
        (enr.case.program.name if enr.case and enr.case.program_id else "")
        or enr.program_name
    )
    return product_type_kind_for_name(program_name)


def _awaiting_enrollments(kind):
    """Enrollments awaiting kitchen assignment for a given product ``kind``
    (meals/boxes). Mirrors the Logistics queue (stage=kitchen_assignment)
    filtered to the kind, which is derived from the program name so meals/boxes
    never mix."""
    qs = (
        EnrollmentVerification.objects.filter(stage=EnrollmentStage.KITCHEN_ASSIGNMENT)
        .select_related("client", "case", "case__program")
    )
    return [e for e in qs if _enrollment_kind(e) == kind]


def _awaiting_box_enrollments():
    """Box households awaiting kitchen assignment (see :func:`_awaiting_enrollments`)."""
    return _awaiting_enrollments(ProductTypeKind.BOXES)


def _box_cadence():
    """The delivery cadence used for boxes (they ship a fixed weekly Wednesday
    schedule regardless, but a valid cadence value is still required)."""
    opts = cadence_options_for_kind(ProductTypeKind.BOXES)
    return opts[0]["value"] if opts else DeliveryCadence.ONCE_A_WEEK


def _prefetched_kitchens():
    """Kitchens with their offered menus + restrictions prefetched, for reuse
    across serviceability checks in one request."""
    return list(
        Kitchen.objects.all().prefetch_related(
            "kitchen_menu_types__menu_type",
            "kitchen_menu_types__restrictions",
        )
    )


def enrollment_ready_for_assignment(enr, kitchens):
    """Whether a household enrollment is 'Ready to assign' -- the same readiness
    shown on the Logistics list: a delivery address is set, every member has a
    menu type and isn't predicted Out of Orbit, and some single kitchen can serve
    every member. Used by the bulk-assign 'only ready to assign' option."""
    if enr.delivery_address_id is None:
        return False
    members = list(enr.member_profiles.all())
    if not members:
        return False
    required = required_product_for_program(enr.program_name)
    serving_sets = []
    for mp in members:
        out, _ = predict_member_out_of_orbit(mp)
        if out:
            return False
        serving_sets.append({
            sk["kitchen"].pk
            for sk in serving_kitchens_for_member(
                mp, kitchens=kitchens, required_product=required,
            )
        })
    return bool(set.intersection(*serving_sets))


class BulkAssignBoxesView(PortalAPIView):
    """Logistics: bulk-assign the single box kitchen to every household awaiting
    kitchen assignment for a boxes program.

    GET returns the box-capable kitchens (for the agent to pick from) and the
    number of boxes households currently awaiting assignment. POST body
    ``{kitchen_id}`` runs the SAME kitchen-output rules + activation as the
    single-household assignment for each, one independent transaction per
    household so one failure never rolls back the rest.
    """

    def get(self, request):
        kitchens = [
            {"id": str(k.pk), "name": k.name, "status": k.status}
            for k in Kitchen.objects.filter(
                supported_products__contains=[KitchenProductType.BOX]
            ).order_by("name")
        ]
        box_enr = _awaiting_box_enrollments()
        kitchens_ctx = _prefetched_kitchens()
        return Response({
            "kitchens": kitchens,
            "awaiting_count": len(box_enr),
            "ready_count": sum(
                1 for e in box_enr if enrollment_ready_for_assignment(e, kitchens_ctx)
            ),
        })

    def post(self, request):
        kitchen_id = request.data.get("kitchen_id")
        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        if kitchen is None:
            return Response(
                {"error": "kitchen_id is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if KitchenProductType.BOX not in (kitchen.supported_products or []):
            return Response(
                {"error": f"{kitchen.name} does not make boxes."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        cadence = _box_cadence()
        agent = current_agent(request)
        enrollments = _awaiting_box_enrollments()
        # Optionally restrict to households that are Ready to assign (matches the
        # Logistics list's readiness): skip any with blockers.
        if request.data.get("ready_only"):
            kitchens_ctx = _prefetched_kitchens()
            enrollments = [
                e for e in enrollments
                if enrollment_ready_for_assignment(e, kitchens_ctx)
            ]

        assigned, out_of_orbit, failed, errors = 0, 0, 0, []
        for enr in enrollments:
            try:
                with transaction.atomic():
                    result = assign_kitchen_to_household(
                        enr, enr.client, kitchen, cadence=cadence, agent=agent,
                    )
                assigned += 1
                out_of_orbit += result.get("out_of_orbit", 0)
            except Exception as exc:  # isolate a bad household; keep going
                failed += 1
                errors.append({
                    "client_id": str(enr.client_id) if enr.client_id else None,
                    "enrollment": enr.code,
                    "error": str(exc),
                })

        return Response({
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
            "total": len(enrollments),
            "assigned": assigned,
            "out_of_orbit": out_of_orbit,
            "failed": failed,
            "errors": errors,
        })


def _household_name(enr):
    """Display name for a household: the primary client's name, else client id."""
    c = enr.client
    if c is not None:
        name = f"{c.first_name or ''} {c.last_name or ''}".strip()
        if name:
            return name
    return str(enr.client_id) if enr.client_id else enr.code


_FOOD_ALLERGY_LABELS = dict(FoodAllergy.choices)


def _member_allergy_labels(profile):
    """Human-readable food-allergy labels for a member (drops the no-op 'none')."""
    return [
        _FOOD_ALLERGY_LABELS.get(c, c)
        for c in (profile.food_allergies or [])
        if c and c != "none"
    ]


def _preview_household_for_kitchen(enr, kitchen, offered):
    """Dry-run the kitchen-output rules for every member of ``enr`` against
    ``kitchen`` WITHOUT saving, so we can show the agent who would end up Out of
    Orbit before committing. Uses the exact same resolver the apply path runs.

    Returns ``{client_id, name, member_count, out_members, fully_covered}`` where
    each out member carries their menu type + allergies so the agent can see WHY.
    """
    out_members = []
    members = list(enr.member_profiles.select_related("client").all())
    for m in members:
        out, _became, reason = reconcile_member_kitchen_output(
            m, kitchen, offered=offered, save=False,
        )
        if out:
            out_members.append({
                "name": m.member_name or "Member",
                "reason": reason,
                "menu_type": m.menu_type or "",
                "allergies": _member_allergy_labels(m),
            })
    return {
        "client_id": str(enr.client_id) if enr.client_id else None,
        "name": _household_name(enr),
        "member_count": len(members),
        "out_members": out_members,
        "fully_covered": not out_members,
    }


class BulkAssignMealsView(PortalAPIView):
    """Logistics: preview + bulk-assign a meals kitchen to households awaiting
    kitchen assignment for a meals program.

    Unlike boxes, meals kitchens differ in menu/allergy coverage, so this is a
    review-first flow:

    * ``GET``  -> meal-capable kitchens, meals cadence options, awaiting count.
    * ``POST`` ``{kitchen_id, cadence, once_a_week_weekday?, preview: true}``
      -> a dry run (NO writes) reporting, per household, who would be set Out of
      Orbit by the chosen kitchen.
    * ``POST`` ``{kitchen_id, cadence, once_a_week_weekday?, only_covered}``
      -> applies. ``only_covered`` (default true) skips households the kitchen
      can't fully serve; set false to assign anyway (excluded members go Out of
      Orbit). Runs the SAME output rules + activation as the single assignment,
      one transaction per household.
    """

    def get(self, request):
        kitchens = [
            {"id": str(k.pk), "name": k.name, "status": k.status}
            for k in Kitchen.objects.filter(
                supported_products__contains=[KitchenProductType.MEAL]
            ).order_by("name")
        ]
        meal_enr = _awaiting_enrollments(ProductTypeKind.MEALS)
        kitchens_ctx = _prefetched_kitchens()
        return Response({
            "kitchens": kitchens,
            "cadence_options": cadence_options_for_kind(ProductTypeKind.MEALS),
            "awaiting_count": len(meal_enr),
            "ready_count": sum(
                1 for e in meal_enr if enrollment_ready_for_assignment(e, kitchens_ctx)
            ),
        })

    def _validated_inputs(self, request):
        """Shared kitchen + cadence validation. Returns (kitchen, cadence,
        once_weekday, error_response)."""
        kitchen_id = request.data.get("kitchen_id")
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None

        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        if kitchen is None:
            return None, None, None, Response(
                {"error": "kitchen_id is required."}, status=http.HTTP_400_BAD_REQUEST
            )
        if KitchenProductType.MEAL not in (kitchen.supported_products or []):
            return None, None, None, Response(
                {"error": f"{kitchen.name} does not make meals."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence not in DeliveryCadence.values:
            return None, None, None, Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence == DeliveryCadence.ONCE_A_WEEK and not once_weekday:
            return None, None, None, Response(
                {"error": "once_a_week_weekday is required for a weekly cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        return kitchen, cadence, once_weekday, None

    def post(self, request):
        kitchen, cadence, once_weekday, err = self._validated_inputs(request)
        if err is not None:
            return err

        enrollments = _awaiting_enrollments(ProductTypeKind.MEALS)
        # Optionally restrict to households that are Ready to assign (matches the
        # Logistics list's readiness). Applies to both the preview and the apply.
        if request.data.get("ready_only"):
            kitchens_ctx = _prefetched_kitchens()
            enrollments = [
                e for e in enrollments
                if enrollment_ready_for_assignment(e, kitchens_ctx)
            ]
        offered = kitchen_offered_menu_index(kitchen)

        # Preview: dry-run only, report households that would have exclusions.
        if request.data.get("preview"):
            fully_covered, with_exclusions, households = 0, 0, []
            for enr in enrollments:
                prev = _preview_household_for_kitchen(enr, kitchen, offered)
                if prev["fully_covered"]:
                    fully_covered += 1
                else:
                    with_exclusions += 1
                    households.append(prev)
            return Response({
                "kitchen_id": str(kitchen.pk),
                "kitchen_name": kitchen.name,
                "total": len(enrollments),
                "fully_covered": fully_covered,
                "with_exclusions": with_exclusions,
                # Only the households needing attention (keeps the payload bounded).
                "households": households,
            })

        # Apply.
        only_covered = request.data.get("only_covered", True)
        agent = current_agent(request)
        assigned, skipped, out_of_orbit, failed, errors = 0, 0, 0, 0, []
        for enr in enrollments:
            if only_covered:
                prev = _preview_household_for_kitchen(enr, kitchen, offered)
                if not prev["fully_covered"]:
                    skipped += 1
                    continue
            try:
                with transaction.atomic():
                    result = assign_kitchen_to_household(
                        enr, enr.client, kitchen, cadence=cadence,
                        once_weekday=once_weekday, agent=agent,
                    )
                assigned += 1
                out_of_orbit += result.get("out_of_orbit", 0)
            except Exception as exc:  # isolate a bad household; keep going
                failed += 1
                errors.append({
                    "client_id": str(enr.client_id) if enr.client_id else None,
                    "enrollment": enr.code,
                    "error": str(exc),
                })

        return Response({
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
            "total": len(enrollments),
            "assigned": assigned,
            "skipped": skipped,
            "out_of_orbit": out_of_orbit,
            "failed": failed,
            "errors": errors,
        })


class MemberKitchenView(PortalAPIView):
    """Change the household's assigned kitchen from the member profile editor.

    The assignment is household-wide: it updates the enrollment and any existing
    delivery-plan snapshots. PATCH body: ``{kitchen_id}`` (null clears it)."""

    def patch(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        kitchen_id = request.data.get("kitchen_id")
        kitchen = get_object_or_404(Kitchen, pk=kitchen_id) if kitchen_id else None
        enr.kitchen = kitchen
        enr.save(update_fields=["kitchen"])
        enr.delivery_schedules.update(kitchen=kitchen)
        # Also refresh the already-generated future delivery occurrences so PO
        # generation groups this household under the NEW kitchen (the calendar
        # snapshots the kitchen at build time and is otherwise never rebuilt).
        resync_scheduled_orders(enrollment=enr)
        return Response({
            "kitchen_id": str(kitchen.pk) if kitchen else None,
            "kitchen_name": kitchen.name if kitchen else "",
        })


class MemberCadenceView(PortalAPIView):
    """Change the household's delivery cadence from the member profile editor.

    Household-wide: recomputes the delivery plan (weekdays, first delivery,
    per-delivery quantity, totals) on every existing schedule. Boxes keep their
    fixed Wednesday schedule. PATCH body: ``{cadence, once_a_week_weekday?}``."""

    @transaction.atomic
    def patch(self, request, client_id):
        client, enr, err = _logistics_enrollment(client_id)
        if err is not None:
            return err
        cadence = (request.data.get("cadence") or "").strip()
        once_weekday = (request.data.get("once_a_week_weekday") or "").strip() or None
        if cadence not in DeliveryCadence.values:
            return Response(
                {"error": "A valid cadence is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if cadence == DeliveryCadence.ONCE_A_WEEK and not once_weekday:
            return Response(
                {"error": "once_a_week_weekday is required for a weekly cadence."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        case = enr.case or s.primary_case(client)
        update_household_cadence(
            enr, cadence=cadence, once_a_week_weekday=once_weekday, case=case
        )
        # A cadence change moves the delivery DATES, so the existing dated
        # calendar must be rebuilt (not just field-resynced): drop future
        # occurrences no longer in the plan and add the new ones, leaving any
        # date already batched into a PO untouched.
        sync_delivery_calendar(enr)
        return Response({
            "cadence": current_household_cadence(enr) or cadence,
            "cadence_label": dict(DeliveryCadence.choices).get(cadence, ""),
        })


class MemberDiagnosticView(PortalAPIView):
    """GET: a read-only service-readiness diagnostic for a client.

    Returns a grouped checklist (coverage, case, lifecycle, verification,
    logistics, tickets) with per-check status (ok/warn/fail/na) plus an overall
    ``ready_for_service`` flag and the list of blocking checks. Never mutates
    state. See api.services.client_diagnostic.diagnose_client.
    """

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        return Response(diagnose_client(client))

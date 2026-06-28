"""Member-scoped portal endpoints: list, detail, and the profile sub-tabs
(insurance, social coverage, history, orders, household, notes, tickets) plus
the verification wizard write."""

import uuid
from datetime import datetime

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    Address,
    Case,
    Client,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    Kitchen,
    MemberDietaryProfile,
    MemberStatus,
    Note,
    NoteSource,
    PurchaseOrder,
    ServiceAuthorizationStatus,
    StageEvent,
    Ticket,
    TimelineEvent,
)
from ..services.catalog import menu_type_for_member, product_type_kind_for_name
from ..services.delivery import (
    cadence_options_for_kind,
    create_member_delivery_schedules,
    current_household_cadence,
    update_household_cadence,
)
from ..services.orders import generate_delivery_calendar
from ..services.kitchens import kitchen_options
from ..services.meal_rules import apply_to_member
from ..services.lifecycle import InvalidTransition, advance_enrollment
from ..services import timeline
from .base import PortalAPIView, PortalGenericAPIView, current_agent
from . import serializers as s

# Reverse of serializers._STATUS_MAP: a filter value -> the lifecycle stages it covers.
STATUS_TO_STAGES = {
    "Denied": ["not_eligible"],
    "Pending": ["pending_verification", "waiting_authorization"],
    "Verified": ["verified", "authorized"],
    "Kitchen Assignment": ["kitchen_assignment"],
    "Active": ["active"],
    "Completed": ["completed"],
}

# Page-level base scope: restricts the list to the lifecycle stages a given
# work area cares about (independent of the per-status filter chips).
SCOPE_TO_STAGES = {
    "verification": ["pending_verification", "waiting_authorization"],
    "logistics": ["kitchen_assignment"],
}

MEMBER_LIST_PREFETCH = ("insurances", "military_profile", "enrollments")


def _parse_date(value):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


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

        # Page-level scope (Verification / Logistics) restricts which stages are
        # ever shown, before the per-status filter chips are applied.
        scope_stages = SCOPE_TO_STAGES.get((params.get("scope") or "").strip())
        if scope_stages:
            qs = qs.filter(lifecycle_stage__in=scope_stages)

        status_val = (params.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            stages = STATUS_TO_STAGES.get(status_val)
            if stages:
                qs = qs.filter(lifecycle_stage__in=stages)
            else:
                qs = qs.filter(lifecycle_stage=status_val)

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

        return qs.distinct()

    def _serialize_member(self, client, is_primary, relationship=""):
        data = s.MemberListSerializer(client).data
        data["is_primary"] = is_primary
        data["relationship"] = relationship
        return data

    @staticmethod
    def _service_type_for_client(client):
        """Meals/Boxes kind derived from the client's enrollment program name
        (prefetched). Empty when neither keyword is present."""
        for enr in client.enrollments.all():
            kind = product_type_kind_for_name(enr.program_name)
            if kind:
                return kind
        return ""

    def _build_groups(self):
        """Group the filtered clients into household groups (one row per
        household, plus each household-less client as its own group). A
        household is included whenever ANY of its members match the filters; the
        expanded view then shows ALL of that household's members."""
        clients = list(self.get_queryset())

        household_ids, seen_hh, individuals = [], set(), []
        for c in clients:
            hm = getattr(c, "household_membership", None)
            if hm and hm.household_id:
                if hm.household_id not in seen_hh:
                    seen_hh.add(hm.household_id)
                    household_ids.append(hm.household_id)
            else:
                individuals.append(c)

        groups = []
        if household_ids:
            members = (
                HouseholdMember.objects.filter(household_id__in=household_ids)
                .select_related("household", "client")
                .prefetch_related(
                    "client__insurances", "client__military_profile",
                    "client__enrollments",
                )
                .order_by("-is_primary", "added_at")
            )
            by_hh = {}
            for hm in members:
                by_hh.setdefault(hm.household_id, []).append(hm)
            for hid in household_ids:
                hms = by_hh.get(hid)
                if not hms:
                    continue
                primary_hm = next((h for h in hms if h.is_primary), hms[0])
                member_data = [
                    self._serialize_member(h.client, h.is_primary, h.relationship)
                    for h in hms
                ]
                primary_data = next(
                    (m for m in member_data if m["id"] == str(primary_hm.client_id)),
                    member_data[0],
                )
                groups.append({
                    "id": str(hid),
                    "type": "household",
                    "name": primary_hm.household.name or primary_data["name"],
                    "member_count": len(member_data),
                    "service_type": self._service_type_for_client(primary_hm.client),
                    "primary": primary_data,
                    "members": member_data,
                })

        for c in individuals:
            primary_data = self._serialize_member(c, True)
            groups.append({
                "id": str(c.client_id),
                "type": "individual",
                "name": primary_data["name"],
                "member_count": 1,
                "service_type": self._service_type_for_client(c),
                "primary": primary_data,
                "members": [primary_data],
            })

        groups.sort(key=lambda g: (g["name"] or "").lower())
        return groups

    def get(self, request):
        # Flat mode: one row per individual member (no household grouping),
        # used by the Members page. Otherwise return household groups.
        if request.query_params.get("flat"):
            # Order + paginate in SQL (LIMIT/OFFSET) so we only ever serialize
            # one page; serializing/sorting the whole clients table per request
            # does not scale once the full member base is imported. Lower() on
            # the name columns reproduces the previous case-insensitive
            # "First Last" ordering.
            qs = self.get_queryset().order_by(Lower("first_name"), Lower("last_name"))
            page = self.paginate_queryset(qs)
            data = [s.MemberListSerializer(c).data for c in page]
            return self.get_paginated_response(data)
        page = self.paginate_queryset(self._build_groups())
        return self.get_paginated_response(page)


class MembersStatsView(PortalAPIView):
    def get(self, request):
        qs = Client.objects.all()
        scope_stages = SCOPE_TO_STAGES.get(
            (request.query_params.get("scope") or "").strip()
        )
        if scope_stages:
            qs = qs.filter(lifecycle_stage__in=scope_stages)
        counts = {"total": qs.count()}
        for label, stages in STATUS_TO_STAGES.items():
            counts[label.lower()] = qs.filter(lifecycle_stage__in=stages).count()
        # Raw per-stage counts (powers stage-specific filter chips such as
        # Pending Verification / Waiting Authorization on the Verification page).
        counts["stages"] = {
            row["lifecycle_stage"]: row["n"]
            for row in qs.values("lifecycle_stage").annotate(n=Count("id"))
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
                    "street": addr.street, "city": addr.city,
                    "state": addr.state, "zip": addr.zip,
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
        for field in ("street", "city", "state", "zip"):
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
            {"street": addr.street, "city": addr.city, "state": addr.state, "zip": addr.zip}
        )


class HouseholdMemberEditView(PortalAPIView):
    """PATCH a single household member's dietary info (MemberDietaryProfile)."""

    def patch(self, request, client_id, member_id):
        mv = get_object_or_404(
            MemberDietaryProfile, pk=member_id, enrollment__client_id=client_id
        )
        ser = s.PortalMemberDietaryEditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        # `reactivate` is a control flag, not a model field — handle separately.
        reactivate = data.pop("reactivate", False)
        for field, value in data.items():
            setattr(mv, field, value)

        if reactivate and mv.status == MemberStatus.OUT_OF_ORBIT:
            # Re-run the meal rule against the edited menu type/allergies. Only
            # return the member to Active if the new combination can actually be
            # fulfilled; otherwise the agent must pick a different menu type.
            result, _ = apply_to_member(mv, save=False)
            if result.out_of_orbit:
                return Response(
                    {"error": "Pick a different menu type to activate this member."},
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
            mv.save()

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
    """All cases for a member, for the New-Ticket “related case” dropdown."""

    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        cases = Case.objects.filter(client_id=client_id).order_by("-date_opened")
        return Response(s.PortalCaseOptionSerializer(cases, many=True).data)


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

    On save the household is verified, advancing the enrollment to VERIFIED
    (which drives the client to the "Verified" lifecycle stage). When the
    authorization outcome is "Accepted" the enrollment is advanced straight to
    SERVICE_ACTIVE ("In Service"), bypassing AUTHORIZED so no delivery orders
    are auto-generated — orders are created manually afterwards. Each transition
    is recorded on the client's history (StageEvent + timeline event).
    """

    @transaction.atomic
    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        ser = s.VerificationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        # Delivery address (shared by the household).
        street = data.get("street", "")
        if data.get("apt"):
            street = f"{street} {data['apt']}".strip()
        address = Address.objects.create(
            client=client,
            type="temporary",
            street=street,
            city=data.get("city", ""),
            state=data.get("state", ""),
            zip=data.get("zip", ""),
        )

        household = getattr(
            getattr(client, "household_membership", None), "household", None
        )
        # Start at PENDING_VERIFICATION; the guarded lifecycle transitions below
        # move it forward and write the history rows.
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

        # Completing the wizard IS the verification, so force past the process
        # gate. This records a StageEvent + timeline event and recomputes the
        # client's lifecycle stage to "Verified".
        advance_enrollment(
            enrollment, EnrollmentStage.VERIFIED, force=True,
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

        enr.kitchen = kitchen
        enr.save(update_fields=["kitchen"])

        # Apply the Meal Rules to each member: derive the kitchen meal type +
        # food notes (sent to the kitchen on the PO) or flag the member Out of
        # Orbit. Out-of-orbit members are excluded from schedules + POs.
        agent = current_agent(request)
        actor = f"agent:{agent.agent_code}" if agent and agent.agent_code else ""
        for profile in enr.member_profiles.select_related("client").all():
            _result, became_out = apply_to_member(profile)
            if became_out:
                try:
                    timeline.event_for_out_of_orbit(
                        profile, enrollment=enr,
                        reason="Allergy/menu combination cannot be safely fulfilled.",
                        actor=actor,
                    )
                except Exception:  # never let history-logging break assignment
                    pass

        case = enr.case or s.primary_case(client)
        create_member_delivery_schedules(
            enr, case=case, cadence=cadence, once_a_week_weekday=once_weekday,
            kitchen=kitchen, member_quantities=member_quantities,
        )

        # Expand the per-member plans into the dated delivery calendar
        # (OrderSchedule) so the household shows up for PO generation.
        generate_delivery_calendar(enr)

        advance_enrollment(
            enr, EnrollmentStage.SERVICE_ACTIVE, force=True,
            note=f"Kitchen assigned ({kitchen.name}); service activated.",
        )
        return Response({
            "id": enr.pk,
            "stage": enr.stage,
            "kitchen_id": str(kitchen.pk),
            "kitchen_name": kitchen.name,
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
        return Response({
            "cadence": current_household_cadence(enr) or cadence,
            "cadence_label": dict(DeliveryCadence.choices).get(cadence, ""),
        })

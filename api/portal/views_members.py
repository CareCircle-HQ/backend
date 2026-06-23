"""Member-scoped portal endpoints: list, detail, and the profile sub-tabs
(insurance, social coverage, history, orders, household, notes, tickets) plus
the verification wizard write."""

import uuid
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    Address,
    Case,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    MemberDietaryProfile,
    Note,
    NoteSource,
    PurchaseOrder,
    ServiceAuthorizationStatus,
    Ticket,
    TimelineEvent,
)
from ..services.catalog import menu_type_for_member
from ..services.delivery import create_member_delivery_schedules
from ..services.lifecycle import advance_enrollment
from ..services.orders import generate_delivery_calendar
from .base import PortalAPIView, PortalGenericAPIView, current_agent
from . import serializers as s

# Reverse of serializers._STATUS_MAP: a filter value -> the lifecycle stages it covers.
STATUS_TO_STAGES = {
    "Denied": ["not_eligible"],
    "Pending": ["pending_verification", "waiting_authorization"],
    "Verified": ["verified", "authorized"],
    "Active": ["active"],
    "Completed": ["completed"],
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

        status_val = (params.get("status") or "").strip()
        if status_val and status_val.lower() != "all":
            stages = STATUS_TO_STAGES.get(status_val)
            if stages:
                qs = qs.filter(lifecycle_stage__in=stages)
            else:
                qs = qs.filter(lifecycle_stage=status_val)

        return qs.distinct()

    def _serialize_member(self, client, is_primary, relationship=""):
        data = s.MemberListSerializer(client).data
        data["is_primary"] = is_primary
        data["relationship"] = relationship
        return data

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
                "primary": primary_data,
                "members": [primary_data],
            })

        groups.sort(key=lambda g: (g["name"] or "").lower())
        return groups

    def get(self, request):
        page = self.paginate_queryset(self._build_groups())
        return self.get_paginated_response(page)


class MembersStatsView(PortalAPIView):
    def get(self, request):
        qs = Client.objects.all()
        counts = {"total": qs.count()}
        for label, stages in STATUS_TO_STAGES.items():
            counts[label.lower()] = qs.filter(lifecycle_stage__in=stages).count()
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
        return Response(
            {
                "enrollment": {"id": enr.pk, "code": enr.code, "stage": enr.stage},
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
        if addr is None:
            addr = Address.objects.create(client_id=client_id, type="temporary")
            enr.delivery_address = addr
            enr.save(update_fields=["delivery_address"])
        for field in ("street", "city", "state", "zip"):
            if field in data:
                setattr(addr, field, data[field])
        addr.save()
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
        for field, value in ser.validated_data.items():
            setattr(mv, field, value)
        mv.save()
        return Response(s.PortalHouseholdMemberSerializer(mv).data)


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
        # client/frontend): only an Accepted (APPROVED) case promotes the
        # household straight into service. Any other status leaves the client at
        # "Verified". Going VERIFIED -> SERVICE_ACTIVE skips AUTHORIZED, so no
        # delivery orders are auto-generated — orders are created manually.
        case = s.primary_case(client)
        accepted = bool(
            case and case.service_authorization_status == ServiceAuthorizationStatus.APPROVED
        )
        if accepted:
            advance_enrollment(
                enrollment, EnrollmentStage.SERVICE_ACTIVE, force=True,
                note="Authorization accepted — placed in service.",
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
            # Cadence + authorization window come from the case (passed in
            # explicitly so this does not depend on the case link above).
            create_member_delivery_schedules(enrollment, case=case)
            # Expand the per-member plans into the dated delivery calendar
            # (OrderSchedule rows) that PO generation later aggregates.
            generate_delivery_calendar(enrollment)

        return Response(
            {"id": enrollment.pk, "code": enrollment.code, "stage": enrollment.stage},
            status=http.HTTP_201_CREATED,
        )

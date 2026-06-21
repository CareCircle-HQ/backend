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
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    MemberStatus,
    MemberVerification,
    Note,
    NoteSource,
    PurchaseOrder,
    ServiceAuthorizationStatus,
    Ticket,
    TimelineEvent,
)
from ..services.lifecycle import advance_enrollment
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
        qs = Client.objects.all().prefetch_related(*MEMBER_LIST_PREFETCH)
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

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        data = self.get_serializer(page, many=True).data
        return self.get_paginated_response(data)


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
            "insurances", "military_profile", "addresses", "tickets",
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
        members = enr.member_verifications.select_related(
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
    """PATCH a single household member's dietary info (MemberVerification)."""

    def patch(self, request, client_id, member_id):
        mv = get_object_or_404(
            MemberVerification, pk=member_id, enrollment__client_id=client_id
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


class MemberTicketsView(PortalAPIView):
    def get(self, request, client_id):
        get_object_or_404(Client, pk=client_id)
        qs = (
            Ticket.objects.filter(client_id=client_id)
            .select_related("assigned_to", "client", "case")
            .prefetch_related("notes")
        )
        return Response(s.PortalTicketSerializer(qs, many=True).data)


class MemberVerificationCreateView(PortalAPIView):
    """POST: create an EnrollmentVerification + MemberVerifications + delivery
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
            MemberVerification.objects.create(
                enrollment=enrollment,
                client_id=m.get("client_id"),
                member_name=m.get("member_name", ""),
                dietary_restrictions=m.get("dietary_restrictions", []),
                food_allergies=m.get("food_allergies", []),
                other_dietary_restrictions=m.get("other_dietary_restrictions", ""),
                meal_category=m.get("meal_category", ""),
                menu_type=m.get("menu_type", ""),
                general_verification_notes=m.get("notes", ""),
                status=MemberStatus.VERIFIED,
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

        return Response(
            {"id": enrollment.pk, "code": enrollment.code, "stage": enrollment.stage},
            status=http.HTTP_201_CREATED,
        )

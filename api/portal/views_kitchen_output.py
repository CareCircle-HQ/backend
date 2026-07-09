"""Portal API: Kitchen Output lookup.

Backs the Logistics > Kitchen Output page. Given a single client id, returns
that member's verification dietary inputs (menu type, dietary restrictions,
food allergies, other restrictions) alongside the resolved KITCHEN OUTPUT
(``kitchen_meal_type`` + ``kitchen_food_notes``) that is actually sent to the
kitchen on each Purchase Order.

Read-only. Resolves the member's :class:`~api.models.MemberDietaryProfile` from
their active (household) enrollment, falling back to the most recently updated
profile for the client so a lookup still works for edge cases.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.response import Response

from ..models import Client, MemberDietaryProfile
from . import serializers as s
from .base import PortalAPIView


class KitchenOutputView(PortalAPIView):
    """GET /api/portal/kitchen-output/<client_id>/ -- one member's verification
    dietary inputs + resolved kitchen output."""

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)

        enr = s.active_enrollment(client)
        profile = None
        if enr is not None:
            profile = enr.member_profiles.filter(client_id=client_id).first()
        if profile is None:
            # Fallback: any dietary profile for this client (most recent).
            profile = (
                MemberDietaryProfile.objects.filter(client_id=client_id)
                .order_by("-updated_at")
                .first()
            )
            if profile is not None:
                enr = profile.enrollment

        if profile is None:
            return Response(
                {
                    "found": False,
                    "client_id": str(client_id),
                    "name": s._full_name(client),
                    "message": (
                        "No dietary profile found for this client. They may not "
                        "be verified yet, or aren't part of an active household."
                    ),
                },
                status=http.HTTP_404_NOT_FOUND,
            )

        member = s.PortalHouseholdMemberSerializer(profile).data
        kitchen_name = ""
        if enr is not None and enr.kitchen_id:
            kitchen_name = enr.kitchen.name

        return Response(
            {
                "found": True,
                "client_id": str(client_id),
                "name": member.get("name") or s._full_name(client),
                "status": member.get("status") or "",
                "status_label": member.get("status_label") or "",
                # Verification inputs (as captured on the Household tab).
                "menu_type": member.get("menu_type") or "",
                "meal_category": member.get("meal_category") or "",
                "dietary_restrictions": member.get("dietary_restrictions") or [],
                "food_allergies": member.get("food_allergies") or [],
                "other_dietary_restrictions": member.get("other_dietary_restrictions") or "",
                # Kitchen output (what we actually send to the kitchen per PO).
                "kitchen_meal_type": member.get("kitchen_meal_type") or "",
                "kitchen_food_notes": member.get("kitchen_food_notes") or "",
                "kitchen_name": kitchen_name,
            }
        )

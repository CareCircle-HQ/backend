"""Client phone endpoints: caller-ID reverse lookup + manual assignment.

Powers the CallTools caller-ID flow in the extension:
- ``GET  /api/phones/lookup/?number=`` -> every client a number is tied to
  (household members can share a number, so this may return several).
- ``GET  /api/clients/<client_id>/phones/`` -> a client's stored numbers.
- ``POST /api/clients/<client_id>/phones/`` -> assign a number to a client
  (idempotent on the client + normalized number).
- ``DELETE /api/clients/<client_id>/phones/<client_phone_id>/`` -> unassign.

Matching uses ClientPhone.normalize (last-10 digits), the same convention as
``api.integrations.calltools.presence``.
"""

import logging

from django.utils import timezone
from rest_framework import permissions, status, views
from rest_framework.response import Response

from .models import Client, ClientPhone, ClientPhoneSource, HouseholdMember

logger = logging.getLogger(__name__)


def _phone_dict(phone):
    return {
        "client_phone_id": str(phone.client_phone_id),
        "raw": phone.raw,
        "normalized": phone.normalized,
        "label": phone.label,
        "source": phone.source,
        "is_primary": phone.is_primary,
        "created_at": phone.created_at,
        "last_seen_at": phone.last_seen_at,
    }


def _client_match(phone):
    """Compact client summary for a caller-ID match, including household id."""
    c = phone.client
    household_id = (
        HouseholdMember.objects.filter(client_id=c.client_id)
        .values_list("household_id", flat=True)
        .first()
    )
    return {
        "client_id": str(c.client_id),
        "first_name": c.first_name,
        "last_name": c.last_name,
        "date_of_birth": c.date_of_birth,
        "lifecycle_stage": c.lifecycle_stage,
        "household_id": str(household_id) if household_id else None,
        "phone": {
            "label": phone.label,
            "source": phone.source,
            "is_primary": phone.is_primary,
        },
    }


class PhoneLookupView(views.APIView):
    """GET /api/phones/lookup/?number=<phone>

    Returns every client the number is already assigned to. Used for caller-ID:
    a single number may resolve to multiple household members."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        number = request.query_params.get("number") or ""
        normalized = ClientPhone.normalize(number)
        if not normalized:
            return Response(
                {"number": number, "normalized": "", "match_count": 0, "matches": []}
            )
        phones = (
            ClientPhone.objects.filter(normalized=normalized)
            .select_related("client")
            .order_by("-is_primary", "client__last_name")
        )
        matches = [_client_match(p) for p in phones]
        return Response({
            "number": number,
            "normalized": normalized,
            "match_count": len(matches),
            "matches": matches,
        })


class ClientPhonesView(views.APIView):
    """GET/POST /api/clients/<client_id>/phones/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, client_id):
        if not Client.objects.filter(pk=client_id).exists():
            return Response({"detail": "No such client."}, status=status.HTTP_404_NOT_FOUND)
        phones = ClientPhone.objects.filter(client_id=client_id)
        return Response([_phone_dict(p) for p in phones])

    def post(self, request, client_id):
        client = Client.objects.filter(pk=client_id).first()
        if client is None:
            return Response({"detail": "No such client."}, status=status.HTTP_404_NOT_FOUND)

        number = (request.data.get("number") or "").strip()
        normalized = ClientPhone.normalize(number)
        if not normalized:
            return Response(
                {"detail": "A valid phone number (>= 10 digits) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        source = request.data.get("source") or ClientPhoneSource.AGENT
        if source not in ClientPhoneSource.values:
            source = ClientPhoneSource.AGENT
        label = (request.data.get("label") or "").strip()

        # Idempotent on (client, normalized): assigning the same number again
        # just refreshes last_seen_at rather than erroring on the constraint.
        phone, created = ClientPhone.objects.get_or_create(
            client=client,
            normalized=normalized,
            defaults={"raw": number, "label": label, "source": source},
        )
        phone.last_seen_at = timezone.now()
        if not created and label and not phone.label:
            phone.label = label
        phone.save(update_fields=["last_seen_at", "label"])

        if bool(request.data.get("is_primary")):
            ClientPhone.objects.filter(client=client, is_primary=True).exclude(
                pk=phone.pk
            ).update(is_primary=False)
            phone.is_primary = True
            phone.save(update_fields=["is_primary"])

        return Response(
            _phone_dict(phone),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ClientPhoneDetailView(views.APIView):
    """DELETE /api/clients/<client_id>/phones/<client_phone_id>/ (unassign)."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, client_id, client_phone_id):
        phone = ClientPhone.objects.filter(
            pk=client_phone_id, client_id=client_id
        ).first()
        if phone is None:
            return Response({"detail": "No such phone."}, status=status.HTTP_404_NOT_FOUND)
        phone.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

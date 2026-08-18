"""Settings > Excluded ZIP Codes: manage the delivery-coverage exclusion list.

The Delivery Coverage Eligibility Check sets a member Out of Orbit when their
delivery-address ZIP is in this list. It is admin-editable so the service area
can change without a code change.

GET    — list every excluded ZIP.
POST   — add one ({"zip": "11209", "label": "optional"}).
DELETE — remove one by id.
"""
import re

from rest_framework import status as http
from rest_framework.response import Response

from ..models import AllowedState, ExcludedZipCode, ServiceZipCode
from ..services.state_area import US_STATES, normalize_state
from .base import PortalAPIView

_ZIP_RE = re.compile(r"^\d{5}$")


def _zip_dict(z):
    return {
        "id": z.pk,
        "zip": z.zip,
        "label": z.label,
        "created_at": z.created_at,
    }


class ExcludedZipCodesView(PortalAPIView):
    def get(self, request):
        zips = list(ExcludedZipCode.objects.all())
        return Response(
            {"count": len(zips), "results": [_zip_dict(z) for z in zips]}
        )

    def post(self, request):
        raw = (request.data.get("zip") or "").strip()
        if not _ZIP_RE.match(raw):
            return Response(
                {"zip": "Enter a valid 5-digit ZIP code."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if ExcludedZipCode.objects.filter(zip=raw).exists():
            return Response(
                {"zip": "This ZIP code is already excluded."},
                status=http.HTTP_409_CONFLICT,
            )
        z = ExcludedZipCode.objects.create(
            zip=raw, label=(request.data.get("label") or "").strip()
        )
        return Response(_zip_dict(z), status=http.HTTP_201_CREATED)


class ExcludedZipCodeDetailView(PortalAPIView):
    def delete(self, request, zip_id):
        z = ExcludedZipCode.objects.filter(pk=zip_id).first()
        if z is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        z.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


def _service_zip_dict(z):
    return {
        "id": z.pk,
        "zip": z.zip,
        "borough": z.borough,
        "is_active": z.is_active,
        "created_at": z.created_at,
        "updated_at": z.updated_at,
    }


class ServiceZipCodesView(PortalAPIView):
    """Settings > Service ZIP Codes: the PHS service-area WHITELIST.

    GET  — list every service ZIP (active + inactive), with its borough.
    POST — add one ({"zip": "10001", "borough": "Manhattan"}). Created active.
    """

    def get(self, request):
        zips = list(ServiceZipCode.objects.all())
        return Response(
            {"count": len(zips), "results": [_service_zip_dict(z) for z in zips]}
        )

    def post(self, request):
        raw = (request.data.get("zip") or "").strip()
        if not _ZIP_RE.match(raw):
            return Response(
                {"zip": "Enter a valid 5-digit ZIP code."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if ServiceZipCode.objects.filter(zip=raw).exists():
            return Response(
                {"zip": "This ZIP code is already in the service area."},
                status=http.HTTP_409_CONFLICT,
            )
        z = ServiceZipCode.objects.create(
            zip=raw, borough=(request.data.get("borough") or "").strip(),
            is_active=True,
        )
        return Response(_service_zip_dict(z), status=http.HTTP_201_CREATED)


class ServiceZipCodeDetailView(PortalAPIView):
    """PATCH /settings/service-zip-codes/<id>/ — toggle active (deactivate/
    reactivate) with ``{"is_active": false}``. DELETE — remove entirely."""

    def patch(self, request, zip_id):
        z = ServiceZipCode.objects.filter(pk=zip_id).first()
        if z is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        active = request.data.get("is_active")
        if active is None:
            active = request.data.get("active")
        if not isinstance(active, bool):
            return Response(
                {"is_active": "Provide a boolean is_active."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        z.is_active = active
        z.save(update_fields=["is_active", "updated_at"])
        return Response(_service_zip_dict(z))

    def delete(self, request, zip_id):
        z = ServiceZipCode.objects.filter(pk=zip_id).first()
        if z is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        z.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class AllowedStatesView(PortalAPIView):
    """Settings > Allowed States: the states we accept clients/cases from.

    GET  — every US state (+DC) with an ``enabled`` flag, so the settings page
           can render the full toggle list from one call.
    POST — enable a state ({"code": "NY"}). Idempotent.
    """

    def get(self, request):
        enabled = {s.code.upper() for s in AllowedState.objects.all()}
        results = [
            {"code": code, "name": name, "enabled": code in enabled}
            for code, name in sorted(US_STATES.items(), key=lambda kv: kv[1])
        ]
        return Response({"count": len(enabled), "results": results})

    def post(self, request):
        code = normalize_state(request.data.get("code"))
        if not code:
            return Response(
                {"code": "Enter a valid US state code."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        obj, _ = AllowedState.objects.get_or_create(
            code=code, defaults={"name": US_STATES.get(code, code)}
        )
        return Response(
            {"code": obj.code, "name": obj.name, "enabled": True},
            status=http.HTTP_201_CREATED,
        )


class AllowedStateDetailView(PortalAPIView):
    """DELETE /settings/allowed-states/<code>/ — disable (remove) a state."""

    def delete(self, request, code):
        norm = normalize_state(code)
        obj = AllowedState.objects.filter(code=norm).first() if norm else None
        if obj is None:
            return Response(status=http.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)

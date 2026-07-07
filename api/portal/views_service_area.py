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

from ..models import ExcludedZipCode
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

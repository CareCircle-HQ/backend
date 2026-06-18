"""Server-side proxy for Google Places (New) — powers the extension's
doctor-address autocomplete on the Cases tab.

The Google API key lives only on the backend (``settings.GOOGLE_MAP_KEY``); the
extension calls these authenticated endpoints instead of Google directly, which
keeps the key secret and sidesteps the MV3 content-security-policy/CORS
restrictions on loading Google's Maps JS in an extension page.

Endpoints (both require the usual agent/service auth):
    GET /api/places/autocomplete/?q=<text>   -> {"suggestions": [{place_id, description}]}
    GET /api/places/details/?place_id=<id>    -> {street, city, state, zip, formatted}
"""

import logging

import requests
from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
TIMEOUT = 8
MIN_QUERY_LEN = 3
MAX_SUGGESTIONS = 6


def _key():
    return getattr(settings, "GOOGLE_MAP_KEY", "") or ""


class PlacesAutocompleteView(APIView):
    """Proxy Places Autocomplete (New). Returns at most ``MAX_SUGGESTIONS``
    US address predictions for the typed text."""

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < MIN_QUERY_LEN:
            return Response({"suggestions": []})
        key = _key()
        if not key:
            return Response({"detail": "Places not configured."}, status=503)

        try:
            resp = requests.post(
                AUTOCOMPLETE_URL,
                headers={
                    "X-Goog-Api-Key": key,
                    "Content-Type": "application/json",
                },
                json={"input": q, "includedRegionCodes": ["us"]},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("Places autocomplete request failed: %s", exc)
            return Response({"detail": "Upstream error."}, status=502)

        if resp.status_code >= 400:
            logger.warning("Places autocomplete %s: %s", resp.status_code, resp.text[:200])
            return Response({"detail": "Places error."}, status=502)

        suggestions = []
        for s in (resp.json() or {}).get("suggestions", []):
            pred = s.get("placePrediction") or {}
            pid = pred.get("placeId")
            if not pid:
                continue
            suggestions.append({
                "place_id": pid,
                "description": (pred.get("text") or {}).get("text", ""),
            })
            if len(suggestions) >= MAX_SUGGESTIONS:
                break
        return Response({"suggestions": suggestions})


class PlacesDetailsView(APIView):
    """Proxy Places Details (New) and flatten the address components into the
    street / city / state / zip fields the doctor form expects."""

    def get(self, request):
        place_id = (request.query_params.get("place_id") or "").strip()
        if not place_id:
            return Response({"detail": "place_id is required."}, status=400)
        key = _key()
        if not key:
            return Response({"detail": "Places not configured."}, status=503)

        try:
            resp = requests.get(
                DETAILS_URL.format(place_id=place_id),
                headers={
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": (
                        "addressComponents,formattedAddress,displayName,"
                        "nationalPhoneNumber,internationalPhoneNumber,websiteUri"
                    ),
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("Places details request failed: %s", exc)
            return Response({"detail": "Upstream error."}, status=502)

        if resp.status_code >= 400:
            logger.warning("Places details %s: %s", resp.status_code, resp.text[:200])
            return Response({"detail": "Places error."}, status=502)

        data = resp.json() or {}
        comps = data.get("addressComponents", [])

        def comp(type_, short=False):
            field = "shortText" if short else "longText"
            for c in comps:
                if type_ in c.get("types", []):
                    return c.get(field, "") or ""
            return ""

        street = " ".join(p for p in [comp("street_number"), comp("route")] if p).strip()
        city = comp("locality") or comp("sublocality") or comp("postal_town")
        state = comp("administrative_area_level_1", short=True)
        zip_code = comp("postal_code")

        return Response({
            "street": street,
            "city": city,
            "state": state,
            "zip": zip_code,
            "formatted": data.get("formattedAddress", ""),
            # Present only when the place is a business/POI (e.g. a clinic).
            "name": (data.get("displayName") or {}).get("text", ""),
            "phone": data.get("nationalPhoneNumber", "")
            or data.get("internationalPhoneNumber", ""),
            "website": data.get("websiteUri", ""),
        })

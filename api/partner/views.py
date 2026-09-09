"""Delivery-partner API views (POD ingestion only).

Every view here:

* accepts ONLY partner authentication (``DeliveryPartnerAuthentication``), so an
  agent JWT or CRM token is rejected,
* renders JSON only -- the project enables ``BrowsableAPIRenderer`` globally,
  which would otherwise hand vendor developers an HTML API explorer,
* resolves the delivery order through :func:`_order_for` so a company can only
  ever touch its OWN orders.

Push-only by design: partners submit, they do not list our data.
"""

import base64
import binascii
import logging

from django.utils import timezone
from rest_framework import exceptions, status as http
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..models import DeliveryOrder, PartnerScope
from ..services import import_storage, pod_ingest
from ..services.pod_import import map_pod_status, parse_delivered_at
from .auth import (
    DeliveryPartnerAuthentication,
    HasPartnerScope,
    IsDeliveryPartner,
    authenticate_client,
    client_ip,
    issue_token,
)

logger = logging.getLogger(__name__)

# Statuses a delivery company may report. Everything else on a DeliveryOrder is
# ours to set -- a partner cannot, say, move an order back to "pending".
PARTNER_STATUSES = {"delivered", "failed", "returned", "cancelled"}

# Inline base64 is capped well below the multipart limit: it arrives ~33% larger
# and is buffered in memory as a string before decoding.
MAX_BASE64_BYTES = 8 * 1024 * 1024

_PRESIGN_PREFIX = "pod-inbox"


def error(code, detail, status_code=http.HTTP_400_BAD_REQUEST, **extra):
    """Machine-readable error body: partners integrate against ``code``."""
    body = {"error": code, "detail": detail}
    body.update(extra)
    return Response(body, status=status_code)


class PartnerAPIView(APIView):
    """Base: partner-only auth, JSON-only, throttled as ``partner``."""

    authentication_classes = [DeliveryPartnerAuthentication]
    permission_classes = [IsDeliveryPartner, HasPartnerScope]
    renderer_classes = [JSONRenderer]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "partner"
    required_scope = None


def _order_for(request, order_id):
    """The partner's own delivery order, or ``None``.

    A company must never see another company's order, so an order that exists
    but belongs to someone else is reported as NOT FOUND rather than forbidden:
    a 403 would confirm the id is real.
    """
    return DeliveryOrder.objects.filter(
        pk=order_id, delivery_company=request.user.delivery_company
    ).select_related("member").first()


def _proof_payload(proof, created):
    return {
        "proof_id": proof.pk,
        "created": created,
        "content_hash": proof.content_hash,
        "delivered_at": proof.delivered_at,
    }


def _metadata(data):
    """The delivery metadata shared by every proof/status submission."""
    return {
        "driver": (data.get("driver") or "").strip(),
        "route_id": (data.get("route_id") or data.get("route") or "").strip(),
        "note": (data.get("note") or "").strip(),
    }


def _delivered_at(data):
    """Accept either an ISO ``delivered_at`` or the report-style date + time."""
    raw = (data.get("delivered_at") or "").strip()
    if raw:
        parsed = timezone.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed
    return parse_delivered_at(data.get("delivery_date"), data.get("delivery_time"))


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
class TokenView(APIView):
    """POST /v1/token/ -- exchange client_id + client_secret for a short-lived
    opaque bearer token. The only unauthenticated endpoint."""

    authentication_classes = []
    permission_classes = []
    renderer_classes = [JSONRenderer]
    parser_classes = [JSONParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "partner_token"

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        client_id = (data.get("client_id") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            return error("invalid_request", "client_id and client_secret are required.")

        ip = client_ip(request)
        # Return 401 ourselves rather than letting AuthenticationFailed bubble:
        # this view has no authenticator, so DRF would have no
        # ``WWW-Authenticate`` header to offer and would downgrade it to 403.
        try:
            client = authenticate_client(client_id, client_secret, ip=ip)
        except exceptions.AuthenticationFailed as exc:
            logger.warning("partner token denied for %r from %s", client_id, ip)
            return error("invalid_client", str(exc.detail), http.HTTP_401_UNAUTHORIZED)

        raw, token = issue_token(client, ip=ip)
        logger.info("partner token issued for %s from %s", client.client_id, ip)
        return Response({
            "access_token": raw,
            "token_type": "Bearer",
            "expires_in": int((token.expires_at - timezone.now()).total_seconds()),
            "scope": " ".join(token.scopes or []),
        })


class WhoAmIView(PartnerAPIView):
    """GET /v1/whoami/ -- lets a partner verify a credential without touching
    any data. Intentionally exposes nothing but their own identity."""

    def get(self, request):
        principal = request.user
        return Response({
            "client_id": principal.client.client_id,
            "delivery_company": principal.delivery_company.name,
            "scopes": principal.scopes,
            "token_expires_at": principal.token.expires_at,
        })


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
class DeliveryStatusView(PartnerAPIView):
    """POST /v1/deliveries/<order_id>/status/ -- report the outcome of a
    delivery WITHOUT a photo (e.g. failed / returned)."""

    required_scope = PartnerScope.POD_STATUS
    parser_classes = [JSONParser, FormParser]

    def post(self, request, order_id):
        order = _order_for(request, order_id)
        if order is None:
            return error("order_not_found", "No delivery order for this company.",
                         http.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else {}
        raw_status = (data.get("status") or "").strip()
        mapped = map_pod_status(raw_status)
        if raw_status and (mapped is None or mapped not in PARTNER_STATUSES):
            return error(
                "invalid_status",
                f"status must be one of: {', '.join(sorted(PARTNER_STATUSES))}.",
                allowed=sorted(PARTNER_STATUSES),
            )
        try:
            delivered_at = _delivered_at(data)
        except (ValueError, TypeError):
            return error("invalid_delivered_at",
                         "delivered_at must be an ISO 8601 timestamp.")

        meta = _metadata(data)
        changed = pod_ingest.apply_delivery_outcome(
            order, status=mapped, delivered_at=delivered_at,
            company=request.user.delivery_company,
        )
        # Driver/route/note are proof-level fields; with no image to attach them
        # to we acknowledge them but only the order outcome is persisted.
        return Response({
            "order_id": str(order.pk),
            "status": order.status,
            "delivered_at": order.delivered_at,
            "updated_fields": changed,
            "metadata_received": {k: v for k, v in meta.items() if v},
        })


# ---------------------------------------------------------------------------
# Proofs -- three transports, one ingestion path
# ---------------------------------------------------------------------------
class ProofUploadView(PartnerAPIView):
    """POST /v1/deliveries/<order_id>/proofs/ -- multipart/form-data.

    One or more files under ``file`` (or ``files``), plus the metadata fields.
    Simplest option for a backend that already has the image bytes.
    """

    required_scope = PartnerScope.POD_PHOTO
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, order_id):
        order = _order_for(request, order_id)
        if order is None:
            return error("order_not_found", "No delivery order for this company.",
                         http.HTTP_404_NOT_FOUND)

        files = request.FILES.getlist("file") or request.FILES.getlist("files")
        if not files:
            return error("no_file", "Attach at least one image as 'file'.")

        data = request.data
        meta = _metadata(data)
        try:
            delivered_at = _delivered_at(data)
        except (ValueError, TypeError):
            return error("invalid_delivered_at",
                         "delivered_at must be an ISO 8601 timestamp.")

        results, failures = [], []
        for f in files:
            outcome, proof, err = pod_ingest.store_proof(
                order, f.read(), content_type=f.content_type,
                company=request.user.delivery_company, delivered_at=delivered_at,
                source_report=f"api:{request.user.client.client_id}",
                filename=f.name, **meta,
            )
            if outcome == pod_ingest.FAILED:
                failures.append({"filename": f.name, "detail": err})
            else:
                results.append(_proof_payload(proof, outcome == pod_ingest.CREATED))

        return _proof_response(order, results, failures, delivered_at, meta, request)


class ProofBase64View(PartnerAPIView):
    """POST /v1/deliveries/<order_id>/proofs/base64/ -- inline base64 in JSON.

    ``photos``: a list of ``{filename, content_type, data}`` (or a bare base64
    string). Convenient for backends that would rather not build a multipart
    body; capped lower than multipart since it is buffered in memory.
    """

    required_scope = PartnerScope.POD_PHOTO
    parser_classes = [JSONParser]

    def post(self, request, order_id):
        order = _order_for(request, order_id)
        if order is None:
            return error("order_not_found", "No delivery order for this company.",
                         http.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else {}
        photos = data.get("photos") or data.get("images") or []
        if isinstance(photos, (str, dict)):
            photos = [photos]
        if not photos:
            return error("no_file", "Provide at least one photo in 'photos'.")

        meta = _metadata(data)
        try:
            delivered_at = _delivered_at(data)
        except (ValueError, TypeError):
            return error("invalid_delivered_at",
                         "delivered_at must be an ISO 8601 timestamp.")

        results, failures = [], []
        for item in photos:
            if isinstance(item, str):
                item = {"data": item}
            if not isinstance(item, dict):
                failures.append({"filename": "", "detail": "photo must be an object or string"})
                continue
            raw = (item.get("data") or item.get("base64") or "").strip()
            name = (item.get("filename") or "").strip()
            # Tolerate a data: URL, which several HTTP clients produce.
            if raw.startswith("data:") and "," in raw:
                raw = raw.split(",", 1)[1]
            if len(raw) > MAX_BASE64_BYTES:
                failures.append({"filename": name, "detail": "base64 payload too large"})
                continue
            try:
                blob = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                failures.append({"filename": name, "detail": "invalid base64"})
                continue

            outcome, proof, err = pod_ingest.store_proof(
                order, blob, content_type=(item.get("content_type") or "").strip(),
                company=request.user.delivery_company, delivered_at=delivered_at,
                source_report=f"api:{request.user.client.client_id}",
                filename=name, **meta,
            )
            if outcome == pod_ingest.FAILED:
                failures.append({"filename": name, "detail": err})
            else:
                results.append(_proof_payload(proof, outcome == pod_ingest.CREATED))

        return _proof_response(order, results, failures, delivered_at, meta, request)


class ProofPresignView(PartnerAPIView):
    """POST /v1/deliveries/<order_id>/proofs/presign/ -- hand back presigned S3
    PUT URLs so many/large images upload straight to storage.

    The partner PUTs each URL then calls ``/confirm/`` with the returned keys.
    """

    required_scope = PartnerScope.POD_PHOTO
    parser_classes = [JSONParser]

    def post(self, request, order_id):
        order = _order_for(request, order_id)
        if order is None:
            return error("order_not_found", "No delivery order for this company.",
                         http.HTTP_404_NOT_FOUND)
        if not import_storage.s3_enabled():
            return error("storage_unavailable",
                         "Direct upload is not available; use the multipart endpoint.",
                         http.HTTP_503_SERVICE_UNAVAILABLE)

        data = request.data if isinstance(request.data, dict) else {}
        files = data.get("files") or data.get("photos") or []
        if isinstance(files, dict):
            files = [files]
        if not files:
            return error("invalid_request",
                         "Provide 'files': [{filename, content_type}, ...].")
        if len(files) > 20:
            return error("too_many_files", "At most 20 files per presign request.")

        uploads = []
        for item in files:
            if isinstance(item, str):
                item = {"filename": item}
            name = (item.get("filename") or "photo.jpg").strip()
            ctype = (item.get("content_type") or "image/jpeg").strip()
            # Staged under a per-company inbox; /confirm/ hashes and re-keys the
            # object into the canonical content-addressed location.
            key = import_storage.build_key(
                f"{_PRESIGN_PREFIX}/{request.user.delivery_company_id}/{order.pk}/{name}"
            )
            uploads.append({
                "filename": name,
                "s3_key": key,
                "upload_url": import_storage.presign_put(key, content_type=ctype),
                "content_type": ctype,
            })
        return Response({
            "order_id": str(order.pk),
            "uploads": uploads,
            "expires_in": 900,
            "confirm_url": f"/v1/deliveries/{order.pk}/proofs/confirm/",
        })


class ProofConfirmView(PartnerAPIView):
    """POST /v1/deliveries/<order_id>/proofs/confirm/ -- register objects that
    were uploaded with presigned URLs.

    We re-read each object so it goes through exactly the same hashing and
    de-duplication as the other two transports (a partner cannot bypass it).
    """

    required_scope = PartnerScope.POD_PHOTO
    parser_classes = [JSONParser]

    def post(self, request, order_id):
        order = _order_for(request, order_id)
        if order is None:
            return error("order_not_found", "No delivery order for this company.",
                         http.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else {}
        keys = data.get("s3_keys") or data.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            return error("invalid_request", "Provide 's3_keys' from the presign step.")

        meta = _metadata(data)
        try:
            delivered_at = _delivered_at(data)
        except (ValueError, TypeError):
            return error("invalid_delivered_at",
                         "delivered_at must be an ISO 8601 timestamp.")

        expected_prefix = f"{_PRESIGN_PREFIX}/{request.user.delivery_company_id}/{order.pk}/"
        results, failures = [], []
        for key in keys:
            key = str(key or "").strip()
            # Only keys we issued for THIS company and order: never let a
            # partner name an arbitrary object in our bucket.
            if expected_prefix not in key:
                failures.append({"s3_key": key, "detail": "key was not issued for this order"})
                continue
            try:
                blob, ctype = import_storage.read_bytes(key)
            except Exception as exc:  # noqa: BLE001
                failures.append({"s3_key": key, "detail": f"could not read upload ({exc})"})
                continue

            outcome, proof, err = pod_ingest.store_proof(
                order, blob, content_type=ctype,
                company=request.user.delivery_company, delivered_at=delivered_at,
                source_report=f"api:{request.user.client.client_id}",
                filename=key.rsplit("/", 1)[-1], **meta,
            )
            if outcome == pod_ingest.FAILED:
                failures.append({"s3_key": key, "detail": err})
            else:
                results.append(_proof_payload(proof, outcome == pod_ingest.CREATED))

        return _proof_response(order, results, failures, delivered_at, meta, request)


def _proof_response(order, results, failures, delivered_at, meta, request):
    """Shared reply for all three transports, including the order update that
    accompanies a proof (a photo implies the delivery happened)."""
    changed = []
    if results:
        changed = pod_ingest.apply_delivery_outcome(
            order,
            status=map_pod_status(request.data.get("status")) if request.data.get("status") else None,
            delivered_at=delivered_at,
            company=request.user.delivery_company,
        )
    body = {
        "order_id": str(order.pk),
        "proofs": results,
        "stored": sum(1 for r in results if r["created"]),
        "duplicates": sum(1 for r in results if not r["created"]),
        "status": order.status,
        "updated_fields": changed,
    }
    if failures:
        body["failures"] = failures
    # Partial success is still a 200 with a per-file breakdown; a hard failure
    # (nothing stored at all) is a 400 so the caller retries.
    if failures and not results:
        body["error"] = "no_proof_stored"
        return Response(body, status=http.HTTP_400_BAD_REQUEST)
    return Response(body, status=http.HTTP_201_CREATED if results else http.HTTP_200_OK)

"""Shared proof-of-delivery ingestion: store one image, apply one status.

Both POD entry points funnel through here so they cannot drift:

* ``services.pod_import`` -- the per-company delivery report (CSV) importer,
* the partner API (``api/partner/views.py``) -- vendors pushing POD to us.

The rules that must hold identically for both live here: the S3 key layout, the
content-hash de-duplication (a re-sent image is never stored twice) and which
``DeliveryOrder`` fields a delivery report may update.
"""

import hashlib
import logging

from django.db import IntegrityError, transaction

from ..models import DeliveryOrderProof
from . import import_storage

logger = logging.getLogger(__name__)

# Guard on a single image, matching the CSV importer.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

_EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/heic": "heic",
    "application/pdf": "pdf",
}

# Outcomes of store_proof(): the caller maps these onto its own counters.
CREATED = "created"
DUPLICATE = "duplicate"
FAILED = "failed"


def guess_ext(content_type, name=""):
    """File extension for a stored proof, from the content type then the name."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    tail = (name or "").rsplit("?", 1)[0].rsplit(".", 1)
    if len(tail) == 2 and 1 <= len(tail[1]) <= 5 and tail[1].isalnum():
        return tail[1].lower()
    return "jpg"


def build_proof_key(order_id, digest, ext):
    """Where a proof image lives in our bucket. Content-addressed, so the same
    bytes for the same order always resolve to the same object."""
    return f"pod/{order_id}/{digest[:16]}.{ext}"


def store_proof(
    order,
    data,
    *,
    content_type="",
    company=None,
    driver="",
    route_id="",
    note="",
    delivered_at=None,
    source_url="",
    source_report="",
    filename="",
):
    """Store ONE proof image for ``order`` and record a :class:`DeliveryOrderProof`.

    De-duplicated on sha256 of the bytes: re-sending the same photo is a no-op,
    which is what makes both the CSV re-imports and API retries safe. Returns
    ``(outcome, proof, error)`` where outcome is CREATED / DUPLICATE / FAILED.
    """
    if not data:
        return FAILED, None, "empty file"
    if len(data) > MAX_IMAGE_BYTES:
        return FAILED, None, f"image too large ({len(data)} bytes)"

    digest = hashlib.sha256(data).hexdigest()
    existing = DeliveryOrderProof.objects.filter(
        delivery_order=order, content_hash=digest
    ).first()
    if existing is not None:
        return DUPLICATE, existing, None

    key = build_proof_key(order.pk, digest, guess_ext(content_type, filename or source_url))
    try:
        import_storage.upload_bytes(
            key, data, content_type=content_type or "application/octet-stream"
        )
    except Exception as exc:  # noqa: BLE001 -- storage failures must not 500
        logger.exception("POD upload failed for order %s", order.pk)
        return FAILED, None, f"storage upload failed ({exc})"

    try:
        with transaction.atomic():
            proof = DeliveryOrderProof.objects.create(
                delivery_order=order,
                s3_key=key,
                content_type=(content_type or "")[:100],
                content_hash=digest,
                source_url=(source_url or "")[:2000],
                delivery_company=company,
                source_report=(source_report or "")[:255],
                driver=(driver or "")[:255],
                route_id=(route_id or "")[:255],
                note=note or "",
                delivered_at=delivered_at,
            )
    except IntegrityError:
        # Raced with a concurrent submission of the same image.
        return DUPLICATE, DeliveryOrderProof.objects.filter(
            delivery_order=order, content_hash=digest
        ).first(), None
    return CREATED, proof, None


def apply_delivery_outcome(order, *, status=None, delivered_at=None, company=None):
    """Apply what a delivery report may change on the order itself.

    Deliberately narrow: status, the delivered timestamp and (when we know it)
    the delivery company. Everything else on a DeliveryOrder is ours. Returns
    the list of fields actually changed (empty when nothing moved).
    """
    fields = []
    if status and order.status != status:
        order.status = status
        fields.append("status")
    if delivered_at and order.delivered_at != delivered_at:
        order.delivered_at = delivered_at
        fields.append("delivered_at")
    if company is not None and order.delivery_company_id != company.pk:
        order.delivery_company = company
        fields.append("delivery_company")
    if fields:
        order.save(update_fields=fields)
    return fields

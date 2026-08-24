"""Proof-of-Delivery (POD) ingestion from per-company delivery reports.

Delivery vendors (USP, QARI, ...) send a per-PO CSV after they deliver. Each row
carries the ``ORDER #`` (== :class:`DeliveryOrder.delivery_order_id`), the member
id, a delivery status/date, and one or more PHOTO URLs (the QARI ``Photos`` field
holds several, newline-separated). Those photo URLs are short-lived signed
CloudFront links, so we fetch each image into OUR S3 during the run and record a
:class:`DeliveryOrderProof` row (deduped by content hash). We also update the
:class:`DeliveryOrder` status / delivered_at / delivery_company from the report.

See docs/proof_of_delivery_plan.md. Pure parsing helpers at the top are unit
tested; the importer below has isolated per-row / per-image error handling.
"""

import base64
import csv
import hashlib
import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import (
    DeliveryOrder, DeliveryOrderProof, DeliveryOrderStatus,
)
from . import import_storage

logger = logging.getLogger(__name__)

POD_SOURCE = "delivery_pod"
_NY = ZoneInfo("America/New_York")
_FETCH_TIMEOUT = 10  # seconds per image (fail fast on dead/expired URLs)
_PROGRESS_EVERY = 25  # flush progress to the ImportRun every N downloads
_MAX_WORKERS = 12     # concurrent image downloads (I/O-bound)
_MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB guard

# --- column mapping (company-agnostic) -------------------------------------
# Each canonical field maps to the header names we've seen across vendors,
# normalized (lower-cased, stripped). New vendors usually just add an alias.
_CANDIDATES = {
    "order_id": ["order #", "order#", "order_id", "order", "order number"],
    "member_id": ["member id", "memberid", "member_id"],
    "photos": ["photo pod", "photos", "photo", "pod photos", "photo url"],
    "status": ["delivery status", "status"],
    "date": ["delivery date", "actual start date", "date"],
    "time": ["delivery time", "actual start time", "time"],
    "driver": ["driver id", "driver"],
    "note": ["delivery note", "pod - note", "pod note", "note"],
}

_STATUS_MAP = {
    "completed": DeliveryOrderStatus.DELIVERED,
    "delivered": DeliveryOrderStatus.DELIVERED,
    "failed": DeliveryOrderStatus.FAILED,
    "returned": DeliveryOrderStatus.RETURNED,
    "cancelled": DeliveryOrderStatus.CANCELLED,
    "canceled": DeliveryOrderStatus.CANCELLED,
}

_DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"]
_TIME_FORMATS = ["%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"]

_EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/heic": "heic",
    "application/pdf": "pdf",
}


def _norm(h):
    return (h or "").strip().lower()


def build_header_index(fieldnames):
    """Map canonical field -> the actual header present in this file (or None).
    Company-agnostic: matches any known alias, case-insensitively."""
    present = {_norm(h): h for h in (fieldnames or [])}
    index = {}
    for canonical, aliases in _CANDIDATES.items():
        index[canonical] = next((present[a] for a in aliases if a in present), None)
    return index


def split_photo_urls(value):
    """Split a photos cell into a de-duped list of http(s) URLs. Handles the
    newline-separated multi-URL QARI cell and single-URL cells alike."""
    if not value:
        return []
    out, seen = [], set()
    for tok in str(value).replace("\r", "\n").replace(",", "\n").split("\n"):
        u = tok.strip()
        if u.lower().startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def map_pod_status(raw):
    """Report status string -> DeliveryOrderStatus value, or None to leave the
    order's status unchanged (unknown labels)."""
    return _STATUS_MAP.get(_norm(raw))


def parse_delivered_at(date_str, time_str):
    """Combine a report date + time into a tz-aware datetime (America/New_York).
    Returns None when the date can't be parsed. Time is optional (defaults 00:00)."""
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    d = None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue
    if d is None:
        return None
    t = None
    ts = (time_str or "").strip()
    for fmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(ts, fmt).time()
            break
        except ValueError:
            continue
    dt = datetime.combine(d, t) if t else datetime(d.year, d.month, d.day)
    return dt.replace(tzinfo=_NY)


def url_expiry_epoch(url):
    """Best-effort expiry (unix epoch) of a signed CDN URL. Handles CloudFront
    canned policies (``Expires=<epoch>``) and custom policies (base64 ``Policy``
    with ``DateLessThan.AWS:EpochTime``). Returns None when there's no expiry."""
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:  # noqa: BLE001
        return None
    if q.get("Expires"):
        try:
            return int(q["Expires"][0])
        except (ValueError, TypeError):
            return None
    pol = q.get("Policy")
    if pol:
        try:
            # CloudFront custom-policy base64 swaps +=/ for -_~ ; reverse it.
            s = pol[0].translate(str.maketrans("-_~", "+=/"))
            s += "=" * (-len(s) % 4)
            doc = json.loads(base64.b64decode(s))
            cond = doc["Statement"][0]["Condition"]["DateLessThan"]["AWS:EpochTime"]
            return int(cond)
        except Exception:  # noqa: BLE001 - malformed policy -> unknown expiry
            return None
    return None


def is_url_expired(url, *, now=None):
    """True when a signed URL's expiry is in the past (so fetching is pointless).
    Unknown/absent expiry -> not expired (we still try)."""
    exp = url_expiry_epoch(url)
    if exp is None:
        return False
    return (now if now is not None else time.time()) >= exp


def _guess_ext(content_type, url):
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    # fall back to the URL path extension (before any query string)
    path = url.split("?", 1)[0].rsplit("/", 1)[-1]
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return "jpg"


# --- importer --------------------------------------------------------------
class PodImporter:
    """Ingest one delivery report for a single delivery company."""

    def __init__(self, *, delivery_company=None, source_report="", apply=True,
                 fetch=True, run=None):
        self.company = delivery_company
        self.source_report = (source_report or "")[:255]
        self.apply = apply          # False = dry run (no writes / no fetches)
        self.fetch = fetch          # False = don't download images (matching only)
        # Optional ImportRun to stream progress to (so the UI shows movement on a
        # long report instead of a frozen "running / 0").
        self.import_run = run
        self.stats = {
            "rows": 0, "matched": 0, "unmatched": 0, "member_mismatch": 0,
            "orders_updated": 0, "proofs_created": 0, "proofs_deduped": 0,
            "images_failed": 0, "images_expired": 0,
        }
        self.errors = []

    def _set_progress(self, processed, total):
        """Best-effort write of progress to the ImportRun so the UI shows a live
        bar during the long download phase. Never fails the import."""
        if self.import_run is None:
            return
        try:
            self.import_run.progress_total = total
            self.import_run.processed_count = processed
            self.import_run.save(update_fields=["progress_total", "processed_count"])
        except Exception:  # noqa: BLE001
            pass

    # -- phase 1: resolve orders + update + collect image tasks (DB, main) ---
    def _prepare(self, idx, rows):
        """Resolve every row to its DeliveryOrder (one bulk query), update the
        order's status/delivered_at/company, and return the list of image-fetch
        tasks (after skipping expired URLs and images we already stored)."""
        oids = []
        for row in rows:
            v = (row.get(idx["order_id"]) or "").strip()
            if v and _looks_uuid(v):
                oids.append(v)
        orders = {
            str(o.pk): o
            for o in DeliveryOrder.objects.filter(pk__in=set(oids))
        }
        tasks = []
        for row in rows:
            oid = (row.get(idx["order_id"]) or "").strip()
            if not oid:
                continue
            self.stats["rows"] += 1
            order = orders.get(oid)
            if order is None:
                self.stats["unmatched"] += 1
                continue
            self.stats["matched"] += 1

            member_id = (idx.get("member_id") and row.get(idx["member_id"]) or "").strip()
            if member_id and str(order.member_id or "").lower() != member_id.lower():
                self.stats["member_mismatch"] += 1
                self.errors.append(
                    f"order {oid}: report member {member_id} != order member {order.member_id}"
                )

            status = map_pod_status(idx.get("status") and row.get(idx["status"]))
            delivered_at = parse_delivered_at(
                idx.get("date") and row.get(idx["date"]),
                idx.get("time") and row.get(idx["time"]),
            )
            driver = (idx.get("driver") and row.get(idx["driver"]) or "").strip()
            note = (idx.get("note") and row.get(idx["note"]) or "").strip()

            if self.apply:
                fields = []
                if status and order.status != status:
                    order.status = status; fields.append("status")
                if delivered_at and order.delivered_at != delivered_at:
                    order.delivered_at = delivered_at; fields.append("delivered_at")
                if self.company and order.delivery_company_id != self.company.pk:
                    order.delivery_company = self.company; fields.append("delivery_company")
                if fields:
                    order.save(update_fields=fields)
                    self.stats["orders_updated"] += 1

            if not self.apply or not self.fetch:
                continue
            for url in split_photo_urls(idx.get("photos") and row.get(idx["photos"])):
                if is_url_expired(url):
                    self.stats["images_expired"] += 1
                    continue
                # Already stored this CDN object for the order? (signed query
                # changes per export, so match the stable path) -> skip download.
                path = (urlparse(url).path or "").strip()
                if path and DeliveryOrderProof.objects.filter(
                    delivery_order=order, source_url__contains=path
                ).exists():
                    self.stats["proofs_deduped"] += 1
                    continue
                tasks.append({
                    "order": order, "url": url,
                    "driver": driver, "note": note, "delivered_at": delivered_at,
                })
        return tasks

    # -- phase 2: download one image (network only, NO db -- thread-safe) ----
    @staticmethod
    def _download(task):
        try:
            resp = requests.get(task["url"], timeout=_FETCH_TIMEOUT)
            if resp.status_code >= 400:
                return task, None, "", f"HTTP {resp.status_code}"
            data = resp.content
            if not data:
                return task, None, "", "empty body"
            if len(data) > _MAX_IMAGE_BYTES:
                return task, None, "", f"image too large ({len(data)} bytes)"
            ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            return task, data, ct, None
        except Exception as exc:  # noqa: BLE001
            return task, None, "", str(exc)

    # -- phase 3: dedup + upload + create (DB, main) ------------------------
    def _store(self, task, data, content_type):
        order, url = task["order"], task["url"]
        digest = hashlib.sha256(data).hexdigest()
        if DeliveryOrderProof.objects.filter(
            delivery_order=order, content_hash=digest
        ).exists():
            self.stats["proofs_deduped"] += 1
            return
        ext = _guess_ext(content_type, url)
        key = f"pod/{order.pk}/{digest[:16]}.{ext}"
        try:
            import_storage.upload_bytes(
                key, data, content_type=content_type or "application/octet-stream"
            )
        except Exception as exc:  # noqa: BLE001
            self.stats["images_failed"] += 1
            self.errors.append(f"order {order.pk}: S3 upload failed ({exc})")
            return
        try:
            with transaction.atomic():
                DeliveryOrderProof.objects.create(
                    delivery_order=order, s3_key=key, content_type=content_type,
                    content_hash=digest, source_url=url[:2000],
                    delivery_company=self.company, source_report=self.source_report,
                    driver=(task["driver"] or "")[:255], note=task["note"] or "",
                    delivered_at=task["delivered_at"],
                )
        except IntegrityError:
            self.stats["proofs_deduped"] += 1
            return
        self.stats["proofs_created"] += 1

    def run(self, reader):
        idx = build_header_index(reader.fieldnames)
        if not idx.get("order_id"):
            raise ValueError(
                "Delivery report is missing an 'ORDER #' column "
                f"(headers: {reader.fieldnames})"
            )
        rows = list(reader)
        # Phase 1: resolve + update orders, collect the images to fetch.
        self._set_progress(0, len(rows) or 1)
        tasks = self._prepare(idx, rows)

        # Phase 2: download the images CONCURRENTLY (I/O-bound). The threads only
        # do network work and touch no DB / shared stats, so this is safe and
        # turns an hours-long sequential fetch into minutes. Progress is streamed
        # as each download completes so the UI shows a live bar.
        total = len(tasks) or 1
        self._set_progress(0, total)
        done = 0
        blobs = []
        if tasks:
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
                for task, data, ct, err in pool.map(self._download, tasks):
                    done += 1
                    if err is not None:
                        self.stats["images_failed"] += 1
                        self.errors.append(
                            f"order {task['order'].pk}: image fetch failed ({err})"
                        )
                    else:
                        blobs.append((task, data, ct))
                    if done % _PROGRESS_EVERY == 0:
                        self._set_progress(done, total)
        self._set_progress(done, total)

        # Phase 3: dedup + upload to our S3 + create the proof rows (DB, main).
        for task, data, ct in blobs:
            try:
                self._store(task, data, ct)
            except Exception as exc:  # noqa: BLE001 - never let one image kill the run
                self.stats.setdefault("row_errors", 0)
                self.stats["row_errors"] += 1
                self.errors.append(f"store error: {exc}")
                logger.warning("pod_import store failed: %s", exc, exc_info=True)
        self._set_progress(total, total)
        return self.stats


def _looks_uuid(s):
    import uuid as _uuid
    try:
        _uuid.UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def run_pod_import_from_reader(*, reader, delivery_company=None, source_report="",
                               apply=True, fetch=True, run=None):
    """Convenience: run an importer over an open csv.DictReader; returns the
    importer (with .stats / .errors)."""
    importer = PodImporter(
        delivery_company=delivery_company, source_report=source_report,
        apply=apply, fetch=fetch, run=run,
    )
    importer.run(reader)
    return importer


def run_pod_import_from_bytes(*, data, delivery_company=None, source_report="",
                              apply=True, fetch=True, run=None):
    """Run over raw CSV bytes/str (used by the management command + Celery task)."""
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    return run_pod_import_from_reader(
        reader=reader, delivery_company=delivery_company,
        source_report=source_report, apply=apply, fetch=fetch, run=run,
    )

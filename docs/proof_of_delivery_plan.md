# Proof of Delivery (POD) — Ingestion Plan

Store proof-of-delivery images for each member delivery, sourced from the
per-company delivery reports (CSV) the kitchens/delivery vendors send back after
a Purchase Order ships.

## Analysis

**Anchor = `DeliveryOrder`.** Verified against a live sample: the report's
`ORDER #` column equals `DeliveryOrder.delivery_order_id` (NOT
`OrderSchedule.order_id`), and the report's member id equals
`DeliveryOrder.member` (Client). `DeliveryOrder` already carries
`delivery_company`, `delivered_at`, and `status` (`DeliveryOrderStatus`).

**Reports differ per company** (per-company column map required):

| Canonical field | USP report | QARI report |
| --- | --- | --- |
| order # (→ `delivery_order_id`) | `ORDER #` | `ORDER #` |
| member id (validate `member`) | `Member ID` | `MEMBERID` |
| photos | `Photo POD` | `Photos` |
| status | `Delivery Status` | `Status` |
| delivered date | `Delivery Date` | `Actual Start Date` |
| delivered time | `Delivery Time` | `Actual Start Time` |
| driver | `Driver ID` | `Driver` |
| note | `Delivery Note` | `PoD - Note` |

- The photos field may contain **more than one URL** (QARI: newline-separated).
- QARI photo URLs are **expiring signed CloudFront URLs** (Policy/Signature/
  Key-Pair-Id + `DateLessThan` epoch) → must be **downloaded to our S3 during the
  import run**; storing the raw URL is not durable.
- USP's `Photo POD` may be empty on some reports but can carry URLs on others →
  parse company-agnostically, no-op when empty.

## Decisions

1. **Storage:** a new structured **`DeliveryOrderProof`** child model.
   **Drop** `DeliveryOrder.proof_of_delivery` (JSON) to avoid a dual source of
   truth.
2. **Ingest:** manual upload, reusing the presigned-S3 + Celery CSV-import flow,
   tagged with the delivery company.
3. **Also update the order** from the report: status, delivered_at,
   delivery_company, driver, note.
4. **Company-agnostic photo parsing** (USP + QARI).

## Model — `DeliveryOrderProof`

- `delivery_order` FK → `DeliveryOrder` (`related_name="proofs"`)
- `s3_key`, `file_url` (our bucket), `source_url` (original signed URL, audit)
- `content_type`, `content_hash` (sha256 of the bytes)
- `driver`, `note`, `delivered_at`, `delivery_company` FK, `source_report`
- `created_at`
- unique `(delivery_order, content_hash)` → idempotent re-imports + cross-report
  dedupe

Remove `DeliveryOrder.proof_of_delivery`; update its two readers to serialize
from `do.proofs` (prefetched, via presigned GET URLs):
- `api/portal/serializers.py` (DeliveryOrder serializer, ~line 1935)
- `api/portal/views_delivery_calendar.py` (~line 233)

## Ingestion (reuse import infra)

1. New **"Delivery Report / POD"** import type in Settings → Import, with a
   required **delivery company** selector; presign the file to the `imports/` S3
   prefix (`api/services/import_storage.py`).
2. Celery task `import_delivery_pod` streams the CSV from S3; per row:
   - resolve `ORDER #` → `DeliveryOrder`; validate member id (mismatch recorded,
     not fatal);
   - update order: status (Completed→DELIVERED, Failed→FAILED, Returned→RETURNED),
     `delivered_at` (date+time, tz-aware), `delivery_company`, driver/note;
   - split photos → URL list; for each: GET (timeout) → hash → if `(order, hash)`
     is new, `upload_bytes` to `pod/<delivery_order_id>/<hash>.<ext>` → create
     `DeliveryOrderProof`;
   - per-row + per-image try/except (a dead/expired URL never fails the batch).
3. Wrap in an `ImportRun`-style summary (rows seen / matched / unmatched /
   images fetched / errors). Run the worker `--pool=solo` on macOS (boto3-fork
   segfault).

## Display (frontend)

Render POD thumbnails (presigned GET; bucket is private) on the delivery calendar
and/or member deliveries tab; click to enlarge; show driver/delivered_at/note.

## Open items / risks

- Expired URL at import time → recorded as a fetch error (needs a fresh report).
- Snapshot vs prod: a report newer than the DB snapshot won't match order #s —
  test against prod / a fresh snapshot.
- Confirm a real USP-with-photos report to validate the `Photo POD` URL shape.
- Company identity comes from the upload selector (optionally validated against
  the `…_USP_…` / `…_QARI_…` filename token).

## Build order

1. Model + migration (drop JSON, fix the 2 readers)
2. Company column map + status/date parsing (pure, unit-tested)
3. Celery ingestion + image fetch + dedupe + ImportRun summary
4. Upload UI wiring
5. Calendar / profile display

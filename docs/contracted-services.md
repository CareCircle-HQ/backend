# Contracted Services

A **case** in Unite Us may have one or more **Contracted Services**. In the Unite
Us core API these are `provided_services`; each carries a service authorization
(amount / delivery window / duration) and one or more invoices. CareCircle now
captures, stores, and syncs them. For the broader case schema see
[data-models.md](./data-models.md); for the capture pipeline see
[content-scripts.md](./content-scripts.md).

## Where it lives

| Layer | Location |
|---|---|
| Model | `ContractedService` in `api/models.py` (FK → `Case`). |
| Serializer | `ContractedServiceSerializer` in `api/serializers.py`. |
| API | `ContractedServiceViewSet` (`api/views.py`), routed at `contracted-services/`. |
| Capture | `apiFetchContractedServices()` in `extension/content/uniteus.js`. |
| Save / UI | `buildContractedServicePayloads()` + `renderContractedServices()` in `extension/sidepanel/sidepanel.js`. |

## Model

`ContractedService` is keyed on the source `provided_service` UUID
(`contracted_service_id`) so imports are idempotent upserts, and has
`case = ForeignKey(Case, related_name="contracted_services")`.

| Group | Fields |
|---|---|
| Definition | `name`, `service_type`, `status`, `fee_schedule_program_id`, `fee_schedule_program_name`, `unit_type` |
| Authorization | `service_authorization_id`, `unite_us_authorization_id` (short id), `authorization_status`, `authorized_amount`, `authorized_units`, `service_duration`, `service_starts_at`, `service_ends_at` |
| Invoice | `invoice_number`, `invoice_status`, `invoice_amount`, `invoice_url` (link), `invoiced_at` |
| Metadata | `created_at`, `updated_at`, `import_batch` |

Migration: `api/migrations/0018_contractedservice.py`.

## API

Standard DRF viewset (auth required), same shape as the other resources.

| Method | Path | Description |
|---|---|---|
| GET | `contracted-services/` | List. Filter with `?case=<uuid>` or `?client=<uuid>`. |
| GET | `contracted-services/<uuid>/` | Detail. |
| POST | `contracted-services/` | Create / upsert one. |
| POST | `contracted-services/bulk/` | Bulk upsert (array; per-item errors, 207 on partial). |

Each payload requires `contracted_service_id` and a `case_id` that already
exists (the case is saved first). Example:

```json
[
  {
    "contracted_service_id": "7b332ecb-f916-49c5-851a-8885d75c5ee0",
    "case_id": "ccbb208d-8d43-4766-baec-2c76ee231370",
    "name": "Medically Tailored Meals",
    "service_duration": "20 units (293-307 minutes)",
    "authorized_amount": "$8,736.00",
    "invoice_number": "INV-10423",
    "invoice_url": "https://app.uniteus.io/invoices/…",
    "invoice_status": "PAID"
  }
]
```

## Capture flow (extension)

When the Cases tab is scanned, `buildCaseDetailFromApi()` calls
`apiFetchContractedServices(caseId, creds)`, which:

1. `GET /provided_services?filter[case]={case_id}` — the contracted services
   list. Each record provides `state`, `unit_amount`, `service_duration`
   (+ clock times), `starts_at`/`ends_at`, a `metadata` description, and its
   `program` + `invoices` relationships.
2. `GET /service_authorizations?filter[case]={case_id}` — the case authorization.
   When the case has exactly one, it supplies `short_id`, `state`,
   `approved_unit_amount`, `approved_cents`, the approved delivery window, and
   the `fee_schedule_program` (resolved for name + unit). The provided_service
   has no auth link, so this is matched at the case level.
3. Each `invoices` id is fetched directly (`GET /invoices/{id}`) for number /
   status / amount / link.
4. Returns a normalized array attached to the case detail as
   `contracted_services`, which the side panel renders and posts to
   `/api/contracted-services/bulk/` right after the parent cases are saved.

> The `provided_service`, `service_authorization`, and `invoice` attribute maps
> are all verified against live responses. Because the invoice echoes the
> program + authorization, a provided_service that has an invoice resolves
> almost every field from the invoice alone.

## Detecting the Unite Us API (URL + response)

The endpoints above were found by inspecting the page's own network traffic.
To confirm or discover the request URL **and** the response shape for any data
the facesheet shows:

### Option A — Chrome DevTools (quickest)

1. Open the client's facesheet, then `F12` → **Network** tab.
2. Filter to **Fetch/XHR** and type `core.uniteus.io` in the filter box.
3. Open the **Cases** tab (or the specific section you want) so the page fires
   its requests.
4. Click a request to read:
   - **Headers → General → Request URL** (the endpoint + query filters), and
   - **Response / Preview** (the JSON:API `data[]` with `attributes` and
     `relationships` — these are the field names to map).

### Option B — Export a HAR (shareable, what we used here)

1. In the Network tab, right-click → **Save all as HAR with content**.
2. Search the HAR for the resource name. Endpoints confirmed this way:
   - `GET /v1/provided_services?filter[case]={case_id}` — contracted services.
     **Confirmed attributes:** `state`, `unit_amount`, `service_duration`
     (minutes) + `service_duration_start_time`/`_end_time`, `starts_at` /
     `ends_at`, `created_at` / `updated_at`, and a `metadata` array (e.g.
     `{ field: "specific_support_provided", value: "…" }`). **Relationships:**
     `program`, `case`, `plan`, `place_of_service`, and `invoices` (the invoice
     ids — fetch each by id). Note it does **not** link to the authorization.
   - `GET /v1/service_authorizations?filter[case]={case_id}` — the case's
     authorization(s); applied to a provided_service when the case has exactly
     one (the provided_service carries no auth link).
   - `GET /v1/service_authorizations/{id}` — amount / dates / short id / status.
     **Confirmed attributes:** `state`, `short_id`, `approved_unit_amount`
     (units; `requested_unit_amount` fallback), `approved_cents` (may be `null`
     for unit-based programs; `requested_cents` fallback), `approved_starts_at`
     / `approved_ends_at` (delivery window), plus a `fee_schedule_program`
     relationship.
   - `GET /v1/fee_schedule_programs/{id}` — service definition (name / unit)
   - `GET /v1/invoices/{id}` — invoice. **Confirmed attributes (denormalized
     superset):** `short_id` (invoice #), `invoice_status` (payer disposition,
     e.g. `accepted_by_payer`), `state` (lifecycle, e.g. `active`),
     `total_amount_invoiced` (cents, e.g. `1750` → $17.50), `amount_paid`,
     `approved_at` / `created_at`, plus echoed program fields
     (`fee_schedule_program_id` / `_name` / `_unit`) and authorization fields
     (`service_authorization_short_id`, `service_authorization_approved_unit_amount`,
     `service_authorization_approved_cents`,
     `service_authorization_approved_starts_at` / `_ends_at`). No link/PDF URL
     field — the extension falls back to `{origin}/invoices/{id}`. The provided
     service's `invoices` relationship gives the id(s) to fetch.

> Tip: **Save HAR _with content_** is required to capture response **bodies**.
> A HAR saved without content (as our `network.har` was) records the request
> URLs but not the JSON responses — which is why the contracted-service
> attribute names still need a live response to finalize.

### Option C — The extension's own credential capture

`extension/content/uw_netcapture.js` already observes the page's auth headers
(`Authorization`, `x-employee-id`, `x-provider-id`) for `core.uniteus.io`. The
isolated content script reuses them via `coreGet(path, creds)`, so once you know
a path from Option A/B you can call it directly without navigating the page —
exactly how `apiFetchContractedServices()` works.

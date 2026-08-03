# Plan: Background report exports (fix "All Members export failed")

## Problem
The **Admin › Reports › All Members** export builds a CSV over **every** member
(~41.5k rows on the current snapshot) and takes **~23s** synchronously. It
returns 200 locally but on prod the request exceeds the gateway/gunicorn timeout,
so the browser shows the generic **"Export failed, please try again."** It is a
timeout, not a code bug. Smaller reports (pending verification, cases) are unaffected.

## Goal
Run large report exports **in the background** (Celery worker), store the CSV in
**S3**, and give the agent a **download link** when it's ready — reusing the exact
infrastructure already proven by the async CSV **import** flow
(`api/tasks.py` + `api/services/import_storage.py` + `ImportRun` polling).

Design it **generically** (keyed by `report_key`) and migrate **every export on
the Reports page** to the same background flow — All Members is the urgent one,
but doing them all uniformly means one code path, one UX, and no other report can
hit the same timeout later.

### Reports to migrate (all of them)
Each becomes a `report_key` with a request-independent row generator in
`REPORT_BUILDERS`:

| report_key | current view | params |
|---|---|---|
| `members-by-lead-source` | `MembersByLeadSourceReportView` | `lead_sources[]`, `created_from/to` |
| `members-pending-verification` | `MembersPendingVerificationReportView` | `created_from/to` |
| `all-verifications` | `AllVerificationsReportView` | `requested_from/to` |
| `all-members` | `AllMembersReportView` | `created_from/to` |
| `cases` | `CasesReportView` | (its existing filters) |
| `members-for-po` | `MembersForPurchaseOrderReportView` | delivery date / kitchen params |
| `members-not-served` | `MembersNotServedReportView` | — |
| `unite-us-agents` | `UniteUsAgentsReportView` | — |

Each existing `*.get()` is refactored to delegate to its generator (single source
of truth); the sync endpoints stay as the **no-S3 fallback** and for tiny pulls.

## Existing infrastructure to reuse
- **Celery**: `backend/celery.py`, `@shared_task` in `api/tasks.py` (e.g.
  `process_import`).
- **S3 storage**: `api/services/import_storage.py` — `s3_enabled()`,
  `upload_fileobj(key, fileobj)`, `build_key()`, `presign_put()`,
  `download_to_temp()`, `delete_object()`. **Need to add** `presign_get()` and an
  `exports/` key builder.
- **Polling pattern**: `ImportRun` (status PENDING→RUNNING→COMPLETED/FAILED),
  polled by the Settings › Import UI. Mirror with a `ReportExport` row.

## Backend

### 1. Storage table `ReportExport` (+ `ReportExportStatus`) — `api/models.py`
A single dedicated table stores **every** export job for **all** report types
(keyed by `report_key`). It is both the polling anchor (status the UI watches)
and a durable audit/history of who exported what, when — mirroring how
`ImportRun` anchors the async import flow.

| field | type | notes |
|---|---|---|
| `export_id` | UUID pk | |
| `report_key` | Char (db_index) | which report, e.g. `"all-members"` (from `REPORT_BUILDERS`) |
| `params` | JSON (default dict) | the filters used (e.g. `created_from`/`created_to`, lead sources) |
| `status` | Char(choices, db_index) | `ReportExportStatus`: pending / running / completed / failed |
| `file_key` | Char (blank) | S3 key under `exports/` (blank until done) |
| `filename` | Char | download name, e.g. `all_members_2026-08-03.csv` |
| `row_count` | PositiveInt null | data rows written |
| `error_log` | Text (blank) | populated on failure |
| `requested_by` | FK Agent (null, SET_NULL) | who ran it (management); `related_name="report_exports"` |
| `created_at` / `finished_at` | DateTime | queued + done timestamps |

```python
class ReportExportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
```

- **Meta**: `ordering = ["-created_at"]`; indexes on `report_key`, `status`, and
  `(requested_by, created_at)` so a "my recent exports" list is cheap.
- **Retention**: rows are kept as history; the S3 objects they point at can be
  TTL-cleaned later via `import_storage.delete_object` (see Follow-ups) without
  deleting the audit row.
- Migration: `0169_reportexport`.

### 2. Reusable CSV row generators — `api/portal/report_exports.py` (new)
Extract EVERY report's row building into request-independent generators that
`yield` lists (header first, then one list per row), registered in one table:
```python
REPORT_BUILDERS = {
    "members-by-lead-source": members_by_lead_source_rows,
    "members-pending-verification": members_pending_verification_rows,
    "all-verifications": all_verifications_rows,
    "all-members": all_members_rows,
    "cases": cases_rows,
    "members-for-po": members_for_po_rows,
    "members-not-served": members_not_served_rows,
    "unite-us-agents": unite_us_agents_rows,
}
def all_members_rows(params): ...  # yields header + one list per member
```
Each generator takes a plain `params` dict (no request) and uses
`queryset.iterator(chunk_size=...)` to bound memory. Every existing report view's
`.get()` is refactored to build its response from its generator, so the sync path
and the background path share ONE implementation per report — no divergence.
A `default_filename(report_key)` helper centralizes the download filenames.

### 3. Storage helpers — `api/services/import_storage.py`
- `EXPORTS_PREFIX = "exports"` + `build_export_key(filename)`.
- `presign_get(key, expires=900)` → short-lived download URL.

### 4. Celery task — `api/tasks.py`
```python
@shared_task(bind=True, ignore_result=True)
def generate_report_export(self, export_id): ...
```
- Load `ReportExport`, set RUNNING.
- Look up `REPORT_BUILDERS[report_key]`, write rows to a temp file with
  `csv.writer`, count rows.
- `import_storage.upload_fileobj(file_key, tmp)`, set COMPLETED + `file_key` +
  `row_count` + `finished_at`.
- On any exception: FAILED + `error_log` (best-effort, always saves status).

### 5. Endpoints — `api/portal/views_reports.py` + `urls.py`
- `POST /reports/exports/` `{report_key, params}` (management-gated):
  validate `report_key`, create `ReportExport(pending, requested_by=agent)`,
  enqueue `generate_report_export.delay(id)`, return `{id, status}`.
  - **No S3 configured** (dev): fall back to the existing **synchronous streaming**
    response so the feature still works locally.
- `GET /reports/exports/<id>/` (management-gated): return `{status, filename,
  row_count, error, download_url}` where `download_url` is a presigned GET when
  COMPLETED.
- `PortalReportExportSerializer`.

## Frontend (`ReportsPage.tsx`)
Introduce ONE shared hook/component used by **every** report panel so they behave
identically:
```ts
useReportExport(report_key)  ->  { start(params), status, rowCount, downloadUrl, error, reset }
```
- `start(params)` → `POST /reports/exports/` `{report_key, params}`, stores `id`.
- Polls `GET /reports/exports/<id>/` ~every 2s; button shows **Preparing… (n rows)**
  → **Download CSV** (opens `download_url`) when `completed`; shows `error` on
  `failed`.
- Migrate all eight report panels (Members by Lead Source, Members Pending
  Verification, All Verifications, All Members, Cases, Active Members for PO,
  Members Not on a PO, Unite Us Agents) to call `useReportExport` instead of the
  direct `apiDownload`. Same look/behavior everywhere.
- When the backend responds that it streamed directly (no S3), the hook just
  triggers the download immediately (no polling) — transparent to the user.

## Fallback / environments
- Background flow requires **S3 + a running Celery worker** (same as imports).
- When `s3_enabled()` is false (local dev), the START endpoint streams the CSV
  synchronously instead, so nothing breaks without S3.
- Tests: run the Celery task **synchronously** (`CELERY_TASK_ALWAYS_EAGER`) and
  monkeypatch `import_storage` upload/presign; assert the row generator output +
  the endpoints' status/`download_url` transitions.

## Testing
- Unit: `all_members_rows(params)` yields the correct header + a row per member,
  respects the date filter.
- Task: `generate_report_export` flips PENDING→COMPLETED, sets `file_key` +
  `row_count` (storage mocked).
- API: POST creates a job (or streams when no S3); GET returns status and a
  `download_url` once completed; management-gated (403 otherwise).

## Rollout
1. Deploy backend (migration `0169`) + ensure the **Celery worker** is running
   (it already runs for imports).
2. Deploy frontend.
3. Retest All Members on prod: job starts, completes, downloads.

## Suggested implementation order (all reports)
1. Infra first: `ReportExport` model + migration, `import_storage.presign_get` /
   `build_export_key`, the Celery task, and the two endpoints + serializer.
2. `report_exports.py` with `all_members_rows` + refactor `AllMembersReportView`;
   wire the frontend `useReportExport` hook on the All Members panel end-to-end.
3. Port the remaining seven generators one at a time (each: extract generator,
   refactor its view to use it, switch its panel to `useReportExport`, add a test).

## Follow-ups
- Optional "Recent exports" list (last N `ReportExport` rows with re-download).
- TTL cleanup of old S3 export objects via `delete_object` (management command).

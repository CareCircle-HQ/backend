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

Design it **generically** (keyed by `report_key`) so every large report can opt
into it, starting with All Members.

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

### 1. Model `ReportExport` (+ `ReportExportStatus`) — `api/models.py`
| field | type | notes |
|---|---|---|
| `export_id` | UUID pk | |
| `report_key` | Char | e.g. `"all-members"` |
| `params` | JSON | filters (e.g. `created_from`/`created_to`) |
| `status` | Char(choices) | pending / running / completed / failed |
| `file_key` | Char | S3 key (blank until done) |
| `filename` | Char | e.g. `all_members_2026-08-03.csv` |
| `row_count` | PositiveInt null | data rows written |
| `error_log` | Text | on failure |
| `requested_by` | FK Agent null | who ran it (management) |
| `created_at` / `finished_at` | DateTime | |

Migration: `0169_reportexport`.

### 2. Reusable CSV row generators — `api/portal/report_exports.py` (new)
Extract the per-report row building into request-independent generators that
`yield` lists:
```python
REPORT_BUILDERS = {"all-members": all_members_rows}   # extend later
def all_members_rows(params): ...  # yields header + one list per member
```
Refactor `AllMembersReportView.get()` to delegate to `all_members_rows` (single
source of truth; the sync endpoint stays for small/filtered pulls and as a
non-S3 fallback). Use `Client.objects...iterator(chunk_size=2000)` inside the
generator to bound memory.

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
- `AllMembersReport` "Export all members" → `POST /reports/exports/`
  `{report_key:"all-members", params}`, store the returned `id`.
- Poll `GET /reports/exports/<id>/` every ~2s; show **Preparing… (n rows)** →
  when `completed`, show a **Download CSV** button (opens `download_url`); on
  `failed`, show `error`.
- The date-range ("Export range") path can keep the direct sync download when the
  filtered set is small; the full export always goes background.

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

## Reusability / follow-ups
- Add `report_key`s for the other heavy reports (All Members done first;
  Cases / Members-for-PO / Members-not-served can plug into `REPORT_BUILDERS`).
- Optional: a small "Recent exports" list (last N `ReportExport` rows with
  re-download) and TTL cleanup of old S3 objects via `delete_object`.

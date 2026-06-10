# Django REST API

The backend (`api/` app) exposes a Django REST Framework API. Routing lives in
`api/urls.py` (a `DefaultRouter` plus explicit auth paths), views in `api/views.py`, and
serializers in `api/serializers.py`. For the underlying schema see
[data-models.md](./data-models.md); for auth details see
[authentication.md](./authentication.md).

**Base URL:** `http://127.0.0.1:8000/api/`

## Auth endpoints

| Method | Path | Description |
|---|---|---|
| POST | `auth/register/` | Create a user (public). |
| POST | `auth/token/` | Obtain a JWT access/refresh pair. |
| POST | `auth/token/refresh/` | Refresh a JWT. |
| POST | `auth/token/verify/` | Verify a JWT. |
| GET | `me/` | Current authenticated user info. |
| GET | `health/` | Health check (public) → `{"status": "ok"}`. |

`auth/register/` and `health/` use `AllowAny`. Everything else requires authentication
(`IsAuthenticated` is the default permission class).

## Resource endpoints

All require auth. Each is a router-registered viewset; the upsert-capable ones support
standard CRUD plus a `/bulk/` POST action.

| Resource | Base path | PK field | Notes |
|---|---|---|---|
| Clients | `clients/` | `client_id` (UUID) | CRUD + `bulk`. Nested `addresses`, `insurances`, `military_profile`. |
| Cases | `cases/` | `case_id` (UUID) | CRUD + `bulk`. Client must already exist. |
| Contracted Services | `contracted-services/` | `contracted_service_id` (UUID) | CRUD + `bulk`. Filter by `?case=` / `?client=`. Case must already exist. See [contracted-services.md](./contracted-services.md). |
| Screenings | `screenings/` | `enhanced_screen_id` (UUID) | CRUD + `bulk`. |
| Eligibility | `eligibility/` | `eligibility_id` (UUID) | CRUD + `bulk`. |
| Providers | `providers/` | `provider_id` (UUID) | Read-only. |
| Programs | `programs/` | `program_id` (UUID) | Read-only. |
| Import Batches | `import-batches/` | `id` (int) | CRUD. Records the authenticated user as importer. |

> Resource paths are not nested under a version prefix — e.g. the full client detail URL
> is `http://127.0.0.1:8000/api/clients/<uuid>/`.

## Bulk upsert

`POST /api/<resource>/bulk/` accepts a JSON **array** of records. Each item is validated
and upserted independently — a per-item failure does not abort the batch
(`BulkUpsertMixin` in `api/views.py`). The response shape is:

```json
{
  "received": 3,
  "succeeded": 2,
  "failed": 1,
  "ids": ["<pk>", "<pk>"],
  "errors": [{ "index": 2, "errors": { "field": ["msg"] } }]
}
```

- HTTP **207 Multi-Status** if any item failed.
- HTTP **200 OK** if all items succeeded.

Upserts are keyed on the resource's PK (e.g. `client_id`), so re-sending the same record
updates the existing row rather than creating a duplicate.

## Filtering

The case/screening/eligibility viewsets support filtering by client UUID:

- `GET /api/cases/?client=<uuid>`
- `GET /api/screenings/?client=<uuid>`
- `GET /api/eligibility/?client=<uuid>`

## `reconcile_insurances` flag

When `reconcile_insurances: true` is present in a **client** upsert payload, the client
serializer treats the incoming `insurances` list as authoritative: any stored insurance
**not** present in the incoming list (and not `verified=True`) is marked
`status=inactive` rather than deleted (preserving history). Without the flag, an ordinary
or partial sync never deactivates stored policies. This behavior is covered by tests in
`api/tests.py`.

## Auth schemes

The API accepts either:

- **JWT** — `Authorization: Bearer <access_token>` (from `auth/token/`), or
- **DRF Token** — `Authorization: Token <token>` (the static service-account token used
  by the extension).

Both are configured in `REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES`
(`JWTAuthentication` then `TokenAuthentication`). See
[authentication.md](./authentication.md).

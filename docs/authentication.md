# Authentication & Security

The backend supports two authentication schemes and is configured in
`backend/settings.py` (`REST_FRAMEWORK` and `SIMPLE_JWT`). The Chrome extension uses the
static DRF token; interactive/API users can use JWT. See
[django-api.md](./django-api.md#auth-endpoints) for the endpoints.

## JWT

Powered by `djangorestframework-simplejwt`.

- **Access token lifetime:** `JWT_ACCESS_MINUTES` (default **60 minutes**).
- **Refresh token lifetime:** `JWT_REFRESH_DAYS` (default **7 days**).
- **Rotate refresh tokens:** enabled (`ROTATE_REFRESH_TOKENS = True`).
- **Header:** `Authorization: Bearer <access_token>` (`AUTH_HEADER_TYPES = ("Bearer",)`).

Obtain a pair at `POST /api/auth/token/`, refresh at `POST /api/auth/token/refresh/`,
verify at `POST /api/auth/token/verify/`.

## DRF Token

A static service-account token (DRF `TokenAuthentication`).

- **Header:** `Authorization: Token <token>`.
- Used by the Chrome extension (baked into `config.js`).
- Generated/rotated via `python manage.py create_service_token` (see
  [etl-import.md](./etl-import.md#service-token-create_service_tokenpy)).

Both schemes are registered in
`REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES`
(`JWTAuthentication` first, then `TokenAuthentication`), and the default permission class
is `IsAuthenticated`.

## Extension auth flow

`getConfig()` in `sidepanel.js` reads `uw_config` from `chrome.storage.local` (set via the
settings UI) and falls back to `window.EXT_CONFIG` (the baked token from `config.js`). It
returns `{ backendUrl, token, scheme }`, and requests send
`Authorization: <scheme> <token>` (default scheme `Token`). The baked-token approach is
recommended for production deployments so end users never have to log in. See
[sidepanel.md](./sidepanel.md#auth).

## CORS

Powered by `django-cors-headers` (`corsheaders.middleware.CorsMiddleware`). Allowed
origins come from `CORS_ALLOWED_ORIGINS` (default `http://localhost:3000`). The extension
talks directly to `http://127.0.0.1:8000` (declared in the manifest's `host_permissions`),
which is not subject to CORS for extension `fetch` calls.

## Security notes

- `extension/config.js` is **gitignored** — never commit the service token.
- `DEBUG` defaults to `True` — **must** be set to `False` in production
  (`DJANGO_DEBUG=False`).
- `DJANGO_SECRET_KEY` has an insecure built-in default — **must** be overridden in
  production.
- PII/PHI fields are marked with comments in the models (e.g. name, DOB, phone, email,
  address lines, `answer_value` / `value_string`, `interpersonal_safety_riskscore`). See
  [data-models.md](./data-models.md).
- JWT access tokens are **not** blacklisted on logout
  (`BLACKLIST_AFTER_ROTATION = False`); see [feature-roadmap.md](./feature-roadmap.md).
- The database is SQLite by default — fine for development, not for concurrent production
  use.

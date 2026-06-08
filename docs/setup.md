# Setup

This guide gets the Django backend running and the Chrome extension loaded for local
development. For the big picture once you're running, see [architecture.md](./architecture.md).

## Prerequisites

- Python 3.12+
- Google Chrome (for the unpacked extension)
- A working clone of this repository

## Backend

From the repository root:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py create_service_token --staff   # generates the API token
python manage.py runserver
```

Notes:

- It is recommended to create and activate a virtual environment first
  (e.g. `python -m venv .venv && source .venv/bin/activate`).
- `migrate` creates the local SQLite database at `db.sqlite3` (gitignored).
- `createsuperuser` gives you a login for the Django Admin at
  `http://127.0.0.1:8000/admin/`.
- `create_service_token --staff` prints a DRF **API token** for a service account
  (`ext-service`). The `--staff` flag also grants admin access. Copy the printed
  token — you'll paste it into the extension's `config.js` (next section). See
  [etl-import.md](./etl-import.md#service-token-create_service_tokenpy) and
  [authentication.md](./authentication.md) for details.
- `runserver` starts the API on `http://127.0.0.1:8000/` by default. Verify it with
  `curl http://127.0.0.1:8000/api/health/` → `{"status": "ok"}`.

## Extension

1. Open `chrome://extensions`, enable **Developer mode**.
2. **Load unpacked** → select the `extension/` folder.
3. Copy `extension/config.example.js` → `extension/config.js`, and paste the service
   token from the backend step above as `apiToken`. (`config.js` is gitignored.)
4. Navigate to `https://app.uniteus.io/facesheet/<any-uuid>` and click the extension's
   toolbar icon to open the side panel.

The baked-token config (`config.js`) looks like:

```js
window.EXT_CONFIG = {
  backendUrl: "http://127.0.0.1:8000",
  apiToken: "<paste service token here>",
  authScheme: "Token",
};
```

See [chrome-extension.md](./chrome-extension.md) and [sidepanel.md](./sidepanel.md)
for how the extension uses these values.

## Environment variables

The backend loads a `.env` file at the repository root (via `python-dotenv`). Copy
`.env.example` to `.env` and adjust as needed. All variables are optional in
development — sensible defaults are applied.

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | insecure dev default | Django cryptographic key. **Must** be overridden in production. |
| `DJANGO_DEBUG` | `True` | Debug mode. Set to `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts. |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated allowed CORS origins. |
| `JWT_ACCESS_MINUTES` | `60` | JWT access-token lifetime (minutes). |
| `JWT_REFRESH_DAYS` | `7` | JWT refresh-token lifetime (days). |
| `GHL_PRIVATE_TOKEN` | — | GoHighLevel private integration token (preferred CRM auth). |
| `GHL_CLIENT_ID` | — | GoHighLevel OAuth client ID (fallback). |
| `GHL_CLIENT_SECRET` | — | GoHighLevel OAuth client secret (fallback). |
| `GHL_LOCATION_ID` | — | GoHighLevel location/sub-account ID. |

> The repo's `.env.example` also lists `GHL_SHARED_SECRET`, `UNITEUS_USERNAME`, and
> `UNITEUS_PASSWORD` placeholders. The shipped code paths documented here use the
> variables in the table above; see [etl-import.md](./etl-import.md) and
> [authentication.md](./authentication.md) for how `GHL_*` values are consumed.

See [authentication.md](./authentication.md) for the security implications of these
settings.

# ETL & Import

CareCircle ingests data three ways: bulk CSV import via a standalone script, a
GoHighLevel CRM client, and a management command that mints the extension's service
token. The CSV/CRM scripts live at the repo root; the management command lives in
`api/management/commands/`. For the resulting schema see
[data-models.md](./data-models.md).

## CSV import (`import_client.py`)

Imports a single client (plus their cases and screenings) from Unite Us CSV exports.

```bash
python import_client.py             # imports the default client id
python import_client.py <client_id> # imports a specific client
```

- Reads the **latest** matching file from the `./data/` directory (chosen by sorted glob):
  - `clients_export_*.csv` (keyed by `client_id`)
  - `cases_export_*.csv` (keyed by `client_id`)
  - `screeningsv2_export_*.csv` (keyed by `subject_id == client_id`)
- Uses the **Django ORM directly** (not the DRF serializers) so raw Unite Us values that
  don't match our choice enums (e.g. `marital_status = "undisclosed"`) are imported as-is
  rather than rejected.
- Upserts **Client → Address → Insurance → Cases → Screenings/Eligibility** within a
  single transaction (`@transaction.atomic`).
- Routes each screening row to the `Screening` or `Eligibility` model based on whether the
  source `screen_type` contains `"assess"` or `"eligib"` (`_is_eligibility()`).
- The screening CSV has one row per answer; the importer dedupes to the first row per
  screen. (Answers/questions are not imported by this script.)
- Has a configurable default client id (`DEFAULT_CLIENT_ID`) used when no argument is
  passed.

### Value-parsing helpers

The script defines small helpers to coerce raw CSV strings:

| Helper | Purpose |
|---|---|
| `s(v)` | strip → string |
| `u(v)` | UUID string or `None` (empty → None) |
| `num_int(v)` | int (tolerates float-like strings) or None |
| `dec(v)` | `Decimal` or None |
| `flt(v)` | float or None |
| `boolean(v)` | true/false/None from common truthy/falsy strings |
| `consent_bool(v)` | boolean from `accepted`/`declined`/etc. |
| `d(v)` | date (tolerates a trailing time component) |
| `dt(v)` | timezone-aware datetime |
| `jlist(v)` | JSON list (or `[]`) |

Plus `latest(pattern)` (newest file matching a glob in `./data/`) and `read_rows(path)`
(UTF-8-BOM-tolerant `csv.DictReader`).

> The CSV field-size limit is raised (`csv.field_size_limit(2_000_000)`) for the wide
> screening export.

## GoHighLevel CRM (`crm_import.py`)

A thin client for the GoHighLevel CRM, built on the `gohighlevel-api-client` library
(imported as `from highlevel import HighLevel`).

- **Auth:** prefers `GHL_PRIVATE_TOKEN` (a Private Integration Token, no OAuth flow);
  falls back to OAuth app credentials (`GHL_CLIENT_ID` + `GHL_CLIENT_SECRET`).
- **`make_client()`** returns a configured `HighLevel` instance.
- **`test_connection()`** is an async smoke test that calls
  `contacts.search_contacts_advanced` against `GHL_LOCATION_ID` and prints the result.
  Running the module directly (`python crm_import.py`) executes this test via
  `asyncio.run`.
- **Required GHL scopes:** `contacts.readonly`, `contacts.write` (configured under GHL
  Settings → Private Integrations).

See [authentication.md](./authentication.md) and [setup.md](./setup.md#environment-variables)
for the `GHL_*` environment variables.

## Service token (`create_service_token.py`)

A Django management command (`api/management/commands/create_service_token.py`) that
creates (or rotates) a long-lived DRF token for a non-interactive service account.

```bash
python manage.py create_service_token            # default username: ext-service
python manage.py create_service_token --staff    # also grant is_staff (admin/import UI)
python manage.py create_service_token --rotate   # delete existing token, issue a fresh one
python manage.py create_service_token --username ext-service --rotate
```

- Creates/uses a service user (default `ext-service`) with an unusable password
  (token-only login).
- `--staff` grants `is_staff` so the account can also use the Django Admin / import UI.
- `--rotate` deletes any existing token before issuing a new one.
- Prints the token; paste it into `extension/config.js` as `apiToken` (sent as
  `Authorization: Token <key>`). See
  [chrome-extension.md](./chrome-extension.md#configjs--configexamplejs).

# Side Panel

The side panel (`extension/sidepanel/`) is the extension's UI. It reads the captured
`uw_context`, validates the client against the backend, renders a schema-driven
Captured-vs-CRM comparison, embeds enrollment forms, and provides save and developer
tools. The data it consumes is produced by the [content scripts](./content-scripts.md);
the endpoints it calls are documented in [django-api.md](./django-api.md).

## Tabs

Defined in `sidepanel.html`:

| Tab | ID | Description |
|---|---|---|
| Profile | `profile` | Client summary, snapshot card, Captured-vs-CRM comparison for client / address / insurance / social care coverage. |
| Screening | `screening` | Met Council screenings auto-walk results + save. |
| Eligibility | `eligibility` | Met Council eligibility assessments auto-walk results + save. |
| Cases | `cases` | Met Council cases auto-walk results + save. |
| E-Form | `eform` | Embedded iframe (locked until validated). |
| N-Form | `nform` | Embedded iframe (locked until validated, URL TBD). |
| V-Form | `vform` | Embedded iframe (locked until validated, URL TBD). |
| Data | `data` | Raw detected client, CRM status, full field comparison, developer tools. |

## Auth

`getConfig()` reads `uw_config` from `chrome.storage.local` (set via the settings UI) and
falls back to `window.EXT_CONFIG` (the baked token from `config.js`). It returns
`{ backendUrl, token, scheme }` where `scheme` defaults to `"Token"`. Requests send
`Authorization: <scheme> <token>` — i.e. `Token <token>` (DRF token) or `Bearer <token>`
(JWT). See [authentication.md](./authentication.md).

## Required-fields gate

```js
REQUIRED_FIELDS = [
  { key: "client_id", label: "Client ID" },
  { key: "client_name", label: "Name" },
  { key: "client_dob", label: "Date of Birth" },
];
```

All three must be present (`requiredMet()`) before validation is allowed.

## Consent gate

`consentAccepted()` checks that `currentContext.captured.client.consent_status` matches
`/accept/i`. Without consent (unknown, pending, declined, revoked, or expired), all
rescan and save buttons are disabled with the tooltip *"No consent on file — reload the
Profile to capture consent first"* (`refreshConsentGate()`). The Profile reload button
stays enabled so the coordinator can re-scan to pick up consent.

## Schema-driven comparison

The `SCHEMA` object defines field mappings for `client`, `address`, `insurance`,
`social_care_coverage`, `case`, `screening`, and `eligibility`. Each field has:

- `key` — the backend field name,
- `label` — the display label,
- `aliases` — lowercase substrings used to fuzzy-match the labels scraped from the Unite
  Us page.

Rows are rendered color-coded:

- **green** — value present in both captured and CRM,
- **blue** — captured only,
- **orange** — CRM only,
- **gray** — missing in both.

(See the legend in the Profile tab: Both / Captured only / CRM only / Missing.)

## Client Snapshot card

Shows consent status; Met Council screenings count + last date + total minutes;
eligibility count + last date; cases count (open/closed) + last date; and CRM presence.
Each row has a status mark (✓/✗) and a **Pull** button to trigger a targeted re-scan of
that record type.

## Save flow

- **`saveBtn`** POSTs the full client payload (with nested `addresses`, `insurances`, and
  `military_profile`) to `POST /api/clients/`.
- **`scrSaveBtn`** → `POST /api/screenings/bulk/`
- **`eligSaveBtn`** → `POST /api/eligibility/bulk/`
- **`caseSaveBtn`** → `POST /api/cases/bulk/`

Each save first re-checks the client exists via `GET /api/clients/<client_id>/`. See
[django-api.md](./django-api.md#bulk-upsert) for the bulk response shape.

## Form tabs

```js
FORMS = {
  eform: "https://links.carecirclecs.com/widget/form/fg6YKsPnZCb4qOtzZ1GU",
  nform: "", // TODO: set N-Form URL
  vform: "", // TODO: set V-Form URL
};
GATED_TABS = ["eform", "nform", "vform"];
```

`GATED_TABS` are locked (🔒) until the client is validated. An empty URL shows a
"not configured" notice. `formfill.js` runs inside each iframe and fills the member-ID
field (see [content-scripts.md](./content-scripts.md#formfilljs--member-id-auto-fill)).
The N-Form and V-Form URLs still need to be configured — see
[feature-roadmap.md](./feature-roadmap.md).

## Developer Tools (Data tab)

The **Inspect Page** button runs `inspectPageFn()` via
`chrome.scripting.executeScript` on the active tab. It dumps tabs, expandables, testids,
`data-test-element` values, label/value pairs, tables, coverage-section outlines,
screening-row structure, and screening-detail Q&A. The output is copyable and is handy
when the Unite Us DOM changes and selectors need updating (see
[known-issues.md](./known-issues.md)).

# Feature Roadmap

Known TODOs and planned work, inferred from the current code. For day-to-day
troubleshooting see [known-issues.md](./known-issues.md).

## Currently incomplete / TODOs in code

- **N-Form / V-Form URLs unset** — `FORMS.nform` and `FORMS.vform` in `sidepanel.js` are
  empty strings (`// TODO: set N-Form URL` / `// TODO: set V-Form URL`). Those tabs show a
  "not configured" notice until URLs are added. See [sidepanel.md](./sidepanel.md#form-tabs).
- **`Product` model not yet defined** — `Case.product_id` is a placeholder UUID field
  referencing a future `Product` model.
- **Free-text service taxonomy** — `Case.service_type` / `service_subtype` are indexed
  free-text fields pending a canonical Product/Service model (the Unite Us taxonomy has
  180+ values).
- **Placeholder form-builder models** — `ScreeningForm`, `Questionnaire`, `Assessment`,
  and `AssessmentQuestionnaire` exist as scaffolding for a future form-builder feature
  (see [data-models.md](./data-models.md#placeholder-models-future-form-builder)).

## Suggested next features (based on code structure)

- **GoHighLevel contact sync** — map `Client` fields to GHL contact custom fields and push
  on save (the `crm_import.py` client and `crm_contact_id` field are already in place).
- **Bulk CSV import via the Django Admin UI** — using the `ImportBatch` model that already
  tracks import runs.
- **N-Form and V-Form configuration** — set their URLs and unlock logic.
- **Screening answer export to GHL custom fields.**
- **Multi-user support** with per-user `uw_config` settings.
- **Production deployment guide** — PostgreSQL, gunicorn, nginx, HTTPS.
- **Automated tests for the extension content scripts** — Playwright is already a
  dependency in `requirements.txt`.

## Known limitations

- **DOM scraping is heuristic** — Unite Us DOM changes can break selectors. Key selectors
  include `data-test-element="fname-display"`, `data-testid="payments-profile-view"`, and
  `data-testid="social-insurance-profile-view"`.
- **Auto-walk navigates the live page** — the user must not interact with the Unite Us tab
  during a scan.
- **Hardcoded org filter** — `SCREENING_ORG = "Met Council - SCN - PHS"` in `uniteus.js`
  must be updated if the org name changes.
- **Hardcoded form label** — `TARGET_LABEL = "enrollment platform member id"` in
  `formfill.js` must match the live form's label exactly.
- **SQLite in development** — not suitable for concurrent production use.
- **No JWT blacklist on logout** — `BLACKLIST_AFTER_ROTATION = False`, so access tokens
  remain valid until they expire. See [authentication.md](./authentication.md).

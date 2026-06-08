# Known Issues & Troubleshooting

Operational troubleshooting for common failure modes. For deeper context, cross-reference
[chrome-extension.md](./chrome-extension.md), [content-scripts.md](./content-scripts.md),
[sidepanel.md](./sidepanel.md), and [django-api.md](./django-api.md).

### Side panel doesn't open

- The extension must be loaded **unpacked** (`chrome://extensions` → Developer mode →
  Load unpacked).
- `background.js` only **enables** the side panel on `app.uniteus.io` tabs. On any other
  host the panel stays disabled. Make sure you're on a Unite Us tab and click the toolbar
  icon.

### Client not detected

- The URL must match `/facesheet/<uuid>` or `/contact/<uuid>` (other patterns like
  `/cases/<status>/<uuid>` and `/submission/<uuid>` are also recognised for case/screening
  IDs). See [content-scripts.md](./content-scripts.md#identification--harvesting).
- Inspect `uw_context` in `chrome.storage.local` via DevTools to confirm what was
  captured.

### Validation fails with 401

- The token in `config.js` is wrong or expired. Re-run
  `python manage.py create_service_token` (optionally with `--rotate`) and paste the new
  token. See [etl-import.md](./etl-import.md#service-token-create_service_tokenpy).

### Forms not loading in the iframe

- The declarativeNetRequest rules in `rules/rules.json` must be active (they strip
  `x-frame-options` / `content-security-policy` headers for the form domains). Verify at
  `chrome://extensions` → extension details → the declarativeNetRequest ruleset is
  enabled. See [chrome-extension.md](./chrome-extension.md#dnr-rules-rulesrulesjson).

### Insurance not captured

- Coverage cards (`[data-testid="payments-profile-view"]` /
  `[data-testid="social-insurance-profile-view"]`) render asynchronously. Navigate to the
  Profile tab and wait for the cards to appear before clicking Re-scan.
  (`coverageSectionsPresent()` guards against deactivating stored insurances when the
  section hasn't loaded.)

### Auto-walk stalls

- The scan TTL is 5 minutes (`SCREENING_SCAN_TTL_MS`). Clear stale scan state by
  navigating away from and back to the facesheet (which resets `uw_scr_scan` /
  `uw_elig_scan` / `uw_case_scan`). Don't interact with the Unite Us tab while a scan
  runs.

### `marital_status` (or other enum) import fails

- Unite Us exports values like `"undisclosed"` that don't match the `MaritalStatus` enum.
  Use `import_client.py` (which writes via the ORM directly and bypasses enum validation)
  for raw imports instead of the DRF API. See [etl-import.md](./etl-import.md).

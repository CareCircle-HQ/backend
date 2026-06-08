# Content Scripts

The extension ships three content scripts (`extension/content/`). Together they capture
Unite Us credentials, scrape and enrich client data, and pre-fill embedded forms. See
[chrome-extension.md](./chrome-extension.md) for how they're registered and
[architecture.md](./architecture.md) for the end-to-end flow.

## `uw_netcapture.js` — credential capture (MAIN world)

- Runs in the **MAIN world** at `document_start` so it can wrap `window.fetch` and
  `XMLHttpRequest.prototype` **before** the page's own JavaScript runs.
- Watches for requests to `screenings-ingestion.uniteus.io` or `core.uniteus.io`.
- Extracts the `Authorization`, `x-employee-id`, and `x-provider-id` headers and emits
  them to the isolated content script via:
  ```js
  window.postMessage({ __uw_creds: true, auth, employeeId, providerId, ts }, origin);
  ```
- It **never alters or blocks** requests — it only observes outgoing headers (the request
  body and response are never read).
- `uniteus.js` listens for these `__uw_creds` messages and uses the credentials to make
  its own direct calls to the Unite Us core API.

## `uniteus.js` — the main scraper

This is the workhorse (~3,250 lines). It extracts IDs from the URL, harvests the DOM,
enriches via the Unite Us core API, runs the auto-walk crawler, and publishes the result
to `chrome.storage.local`.

### Identification & harvesting

- **`parseIdsFromUrl()`** — extracts `client_id`, `case_id`, and `screening_id` from the
  URL. Recognised patterns include `/facesheet/<uuid>`, `/contact/<uuid>`,
  `/cases/<status>/<uuid>` (and `/cases/<uuid>`), and `/submission/<uuid>` (and
  `/screenings/<uuid>`). If no client ID is matched, it falls back to the first UUID in
  the path.
- **`harvestFields()`** — generic label→value harvest from `<dl>/<dt>/<dd>`,
  `<tr>/<th>/<td>`, form fields, and `[class*="label"]` elements.
- **`harvestProfile()`** — structured profile harvest using `data-test-element`
  attributes (`fname-display`, `lname-display`, `dob-display`, etc.) plus section-text
  parsing for consent, household size, contact info, and care coordinator.
- **`harvestInsurance()`** — reads `[data-testid="payments-profile-view"]` and
  `[data-testid="social-insurance-profile-view"]` cards; captures plan name, member ID,
  group ID, start/end dates, and status; tags each record with an `active` flag.
- **`coverageSectionsPresent()`** — guard that prevents deactivating stored insurances
  when the coverage section didn't actually load.
- **`collectRecords()` / `harvestTableRecords()`** — scan anchor `href`s for
  case/screening/eligibility UUIDs and parse visible tables into structured row objects.

### Accumulation & enrichment

- **Per-client accumulator (`accum`)** — merges scrapes across page navigations for the
  same `client_id`. Persisted to `chrome.storage.local` as `uw_accum` (`persistAccum()`)
  and restored on page reload (`restoreAccum()`), so data captured on previously visited
  sub-pages isn't lost.
- **`maybeEnrichFromApi()`** — uses the captured Unite Us auth headers to call
  `core.uniteus.io` directly for authoritative demographics, insurance, care coordinator,
  consent, and preferred languages. Throttled; forced on deep scrapes (the Profile
  reload). API values win over the DOM scrape for the fields they cover.

### Auto-walk crawler

- A **resumable state machine** whose state lives in `chrome.storage.local`
  (`uw_scr_scan`, `uw_elig_scan`, `uw_case_scan`). It walks the Screenings / Eligibility
  Assessments / Cases facesheet tabs, clicks each row, harvests the detail page, and
  navigates to the next. Survives page reloads.
- Filtered to `SCREENING_ORG = "Met Council - SCN - PHS"` — only rows belonging to that
  organization are captured.
- Scan state has a TTL (`SCREENING_SCAN_TTL_MS = 5 * 60 * 1000`, i.e. 5 minutes) so stale
  scans are abandoned.
- **`harvestScreeningDetail()`** — captures question/answer pairs from
  `.ui-form-renderer-question-display` elements (`.__label` / `.__value`) and screening
  results from `.need-card__name`.

### Publishing & cleanup

- **`publishContext()`** — assembles the final `uw_context` object and writes it to
  `chrome.storage.local`. It only writes when the serialized value changes (compared
  against the last serialized payload), avoiding redundant storage churn.
- **`clearClientScopedData()`** — removes `uw_screenings`, `uw_eligibility`, `uw_cases`,
  `uw_scr_scan`, `uw_elig_scan`, and `uw_case_scan` when the client changes, so the panel
  never shows stale data from a previous client.

## `formfill.js` — member-ID auto-fill

Runs inside the embedded form iframes (`scnlp.metcouncil.org/agentforms/*` and
`links.carecirclecs.com/widget/form/*`) in all frames.

- **`TARGET_LABEL = "enrollment platform member id"`** — matched case-insensitively
  against label text, `aria-label`, `placeholder`, `name`, `id`, `title`, and
  `aria-labelledby`.
- **`setNativeValue()`** — a React/Angular-friendly value setter that uses
  `Object.getOwnPropertyDescriptor(proto, "value").set` and then dispatches `input`,
  `change`, and `blur` events so the framework's change detection fires.
- A **`MutationObserver`** watches for dynamically rendered forms and retries; it
  disconnects after 30 seconds to avoid overhead.
- A **`chrome.storage.onChanged`** listener re-fills when `uw_context` changes (i.e. the
  coordinator navigates to a different client).
- The client ID is read from `uw_context.client_id` in `chrome.storage.local`.

If the live form's label wording changes, update `TARGET_LABEL` (see
[known-issues.md](./known-issues.md)).

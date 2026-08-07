# Nightly Assessment Eligibility Pull — Refactor Plan

## Goal

Every night, for **every assessment we ingest**, bring in the assessment's
**`eligible_services`** (the "Client May Be Eligible" program results) and
**`eligible_status`**, so the system automatically:

1. upserts the **program catalog** (`catalog.upsert_programs(eligible_services)`), and
2. derives the client's **Level 1 / Level 2** (`derive_client_level` → `Client.is_level`).

Today only the **browser extension** captures this (per-client, agent-driven).
The nightly pull does not, so most assessments land with empty results and never
drive the catalog or client level.

---

## Current state (as-is)

There are **two** Unite Us ingestion paths, and **neither** brings assessment
results:

### 1. Daily core-API pull — `api/services/uniteus_import.py` (`DailyPull`)
- Hits the Unite Us **core JSON:API** via `api/integrations/uniteus/api.py`
  (`UniteUsApi`), authenticated with a stored `UniteUsCredential`
  (access/refresh token).
- Upserts **Clients, Cases, Insurance, SocialCareCoverage, ContractedService**.
- **Does NOT touch assessments at all** (no assessment code path).

### 2. CSV export automation — `api/services/uniteus_exports.py` + tasks
- `request_uniteus_exports` (nightly) requests rolling-window CSV exports for
  `assessments`, `screeningsv2`, `cases`, `clients`; `poll_uniteus_exports`
  downloads them and feeds each through `run_csv_import` (`api/services/csv_import.py`).
- The **`assessments` CSV export** is how assessments enter the system today.
- **That export has NO results column** — `map_assessment_group` explicitly
  leaves `eligible_status` / `eligible_services` empty (see its docstring). So
  CSV-imported assessments carry Q&A + form_name + submitter/org + date, but
  never results.

### Downstream (already built — no change needed)
`AssessmentSerializer._upsert` already:
- links the `Client`,
- calls `catalog.upsert_programs(obj.eligible_services)`,
- calls `derive_client_level(obj.eligible_services)` → sets `Client.is_level`.

**So the entire downstream already works the moment an assessment has
`eligible_services`.** This refactor is purely about *sourcing* the results
nightly.

### Where the results actually live
The extension gets results from a **different Unite Us host** than the backend
uses today:

```
GET https://screenings-ingestion.uniteus.io/v2/screenings?person_id=<id>&type=assessment&offset=&limit=20
GET https://screenings-ingestion.uniteus.io/v2/screenings/<id>?template_format=surveyjs
```
- Auth: a **short-lived Bearer token** + `x-employee-id` + `x-provider-id`
  headers. The extension captures these from the logged-in browser session
  (MAIN-world netcapture shim) — it does **not** use the core-API credential.
- `eligible_services` comes from the detail's `screen.eligible_services` (or the
  list summary's `eligible_services`); Q&A comes from the SurveyJS `questions`.
- Provider scoping: filter to `organization_id == x-provider-id` (Met Council).

See `extension/content/uniteus.js`: `apiFetchScreeningList`,
`apiFetchScreeningDetail`, `parseApiAssessmentDetail`, `runEligibilityApiScan`.

---

## The core challenge (make-or-break) — now LOW risk

**Can the backend authenticate to `screenings-ingestion.uniteus.io` on its own,
unattended, nightly?** — almost certainly **yes**, reusing the existing auth.

Updated understanding (2026-08-07):
- The Unite Us **auth mechanism already exists** — built for the core
  clients/cases pull we are now retiring: `UniteUsCredential` stores the session
  `access_token` + `refresh_token` + `provider_id` + `employee_id`;
  `integrations/uniteus/client.py` does headless token refresh; and
  `auth_headers(cred)` already emits `Authorization` + `x-employee-id` +
  `x-provider-id`.
- The extension's netcapture shim captures the auth from **both**
  `core.uniteus.io` AND `screenings-ingestion.uniteus.io` and **merges them into
  a single bearer** (its own comment: *"Different hosts send different headers…
  Merge so we never drop an id"*). That strongly implies **one session bearer is
  valid across both hosts** — they differ only in which id header they require
  (`x-provider-id` for ingestion). So the stored core credential should
  authenticate to the ingestion host as-is.

**Residual unknown (small):** confirm the ingestion host accepts the stored token
+ `x-provider-id`/`x-employee-id` headlessly (and refresh behaves). Phase 0 is now
a quick **confirmation probe**, not a fundamental spike.

### Scope note — the core clients/cases pull is being RETIRED
Per product decision, the per-client core-API `daily_pull` (clients + insurance/
Medicaid/coverage + cases) is being **removed** and replaced by **manually-run,
date-windowed CSV imports** (Settings → Import), which are already authoritative
and cover insurance/coverage (denormalized into the `clients` export). Confirmed:
the windowed `clients` export ships each included client's FULL current
insurance/coverage set, so the authoritative reconcile is safe. The ONLY thing
the backend still needs the Unite Us API for is the **assessment/screening
results** — via the ingestion host described here.

---

## Proposed approach (phased)

### Phase 0 — Auth confirmation probe (quick)
- Throwaway management command `probe_assessment_api --client <id>` that calls
  the ingestion endpoints using the **existing** `UniteUsCredential` via
  `auth_headers(cred)` (token + `x-provider-id` + `x-employee-id`) and prints the
  raw response (or the auth failure).
  ```
  GET https://screenings-ingestion.uniteus.io/v2/screenings?person_id=<id>&type=assessment&limit=20
  GET https://screenings-ingestion.uniteus.io/v2/screenings/<id>?template_format=surveyjs
  ```
- Expected: 200 with `eligible_services` on the record (given the shared-bearer
  finding). Confirms GO.
- If it 401s: try a fresh `ensure_fresh(cred)` refresh first; if still 401, the
  ingestion host needs a distinct token — fall back options: (1) request Unite Us
  add `eligible_services` to the CSV `assessments` export; (2) keep extension-only
  results. Document and stop.

### Phase 1 — Backend screenings-ingestion API client
- New module `api/integrations/uniteus/screenings_api.py` (kept separate from the
  core `api.py` since it's a different host/auth):
  - `list_assessments(person_id, provider_id)` — paginated `type=assessment`.
  - `get_assessment_detail(assessment_id)` — `template_format=surveyjs`.
  - Token acquisition/refresh + `x-employee-id` / `x-provider-id` header helper.
- Credential storage: extend `UniteUsCredential` (or a sibling model) to hold the
  screenings-ingestion token + employee/provider ids, if distinct from core.

### Phase 2 — Assessment mapping (list + detail → serializer payload)
- New `mappers.map_assessment_api(detail, summary)` returning the
  `AssessmentSerializer` shape, mirroring the extension's
  `parseApiAssessmentDetail`:
  - `assessment_id` ← record id
  - `subject_id` ← person_id
  - `screen_created_at` ← created date
  - `form_name` ← template/form name (API detail; the CSV export also has this)
  - `provider_name` / `performing_organization_name` ← submitter / org
  - `questions_answers` ← `[{question, answer}]` from SurveyJS questions
  - **`eligible_services`** ← `detail.eligible_services || summary.eligible_services`
  - **`eligible_status`** ← "eligible" when complete (match extension rule)
- Reuse `AssessmentSerializer` (idempotent upsert) → catalog + level "for free".

### Phase 3 — Wire into the nightly orchestration
Decide the host (see Open Questions): most likely **extend `DailyPull`** with an
`import_assessments()` step (it already iterates the clients we track and runs
inside `change_context(IMPORT)`), rather than the CSV export automation.
- Per tracked client (Met Council-scoped): list assessments → fetch each detail →
  map → upsert → count into the `ImportRun`.
- Isolation per record (one bad assessment can't fail the run), matching the
  existing `_save` pattern.
- `emit_side_effects=False` on cron (timeline/tickets stay off, like the rest of
  the pull) — but still recompute stage + drive catalog/level via the serializer.
- Throttle/paginate: N clients × (1 list + M detail) calls; add rate limiting and
  a per-run cap; consider only clients with new/updated assessments.

### Phase 4 — Reconcile the CSV assessments path
Decide (Open Questions): once the API path lands, either
- (a) **retire** the `assessments` CSV export (API is now authoritative), or
- (b) **keep both** — CSV as a coverage backstop, API as the results enricher
  (upsert is idempotent + keyed on `assessment_id`, so they converge; ensure the
  API run doesn't blank a field the CSV set, and vice-versa).

### Phase 5 — Backfill existing assessments
- Management command `backfill_assessment_eligibility [--apply] [--since]` that
  walks existing `Assessment` rows with empty `eligible_services`, fetches results
  from the API, and upserts — so historical assessments retroactively drive the
  catalog + client level. Dry-run by default (matches our command conventions).

### Phase 6 — Verification & rollout
- Unit tests: `map_assessment_api` shape; serializer upsert drives
  `catalog.upsert_programs` + `derive_client_level`; idempotency; provider scoping.
- Integration test with a recorded API fixture.
- Roll out behind a settings flag (e.g. `UNITEUS_ASSESSMENT_API_ENABLED`) so we
  can enable in prod after a dry-run validates auth + volume.

---

## Data-model impact
- **None required** — `Assessment` already has `eligible_services`,
  `eligible_status`, `form_name`, `questions_answers`.
- Possible: new fields on `UniteUsCredential` (or a new credential row) for the
  screenings-ingestion token + employee/provider ids, if the auth is distinct.

---

## Related findings: SCREENINGS (screeningsv2 export)

Comparing the same CSV-vs-extension analysis for **screenings** surfaced a few
things that intersect this refactor:

- **Screenings already carry results in the CSV export.** Unlike the assessments
  export, `screeningsv2_export` HAS `eligible_status` + `eligible_services`
  columns, and `map_screening_group` already imports them. So the nightly CSV
  path already brings screening eligibility — no API refactor needed *for
  screenings* to get the data in.
- **But screening `eligible_services` does NOT drive client Level.**
  `derive_client_level` is only called from `AssessmentSerializer`, never
  `ScreeningSerializer`. Screening `eligible_services` is stored but inert for
  Level 1/2. Decision needed: should screenings also drive client Level, or is
  Level strictly an assessment concept?
- **The extension screening path drops results.** `buildScreeningPayloads` sends
  `identified_social_needs` (need chips) but NOT `eligible_services` /
  `eligible_status` — even though the screenings-ingestion API detail
  (`v2/screenings/<id>`) returns `eligible_services` on every record (the
  assessment path already reads `screen.eligible_services`; screenings use the
  identical endpoint/model). So the extension COULD capture screening
  eligibility with a small change: read `eligible_services` / `eligible_status`
  in the screening detail parser and include them in `buildScreeningPayloads`.
  No backend change — `ScreeningSerializer` already accepts both fields.
- **Extension omits structured need rows.** CSV import also creates
  `IdentifiedSocialNeed` / `VerifiedSocialNeed` rows (codes + dates); the
  extension only sends need NAME strings. Out of scope here, noted for parity.

**Implication for this refactor:** the screenings-ingestion API (Phase 0/1) is
the SAME source for both assessments AND screenings `eligible_services`. If the
auth spike succeeds, we can optionally enrich screenings from the same client in
the same nightly step (bonus), and separately decide whether screenings feed
`derive_client_level`.

---

## Risks & open questions
1. **AUTH (now low):** the existing credential + shared session bearer should
   authorize the ingestion host; Phase 0 is a quick confirmation probe.
2. **Person discovery / scale (now bounded):** the list endpoint is per
   `person_id`, BUT we only need results for records **missing** them (immutable
   once set), so nightly volume = just the new assessments/screenings from that
   day's CSV import — a small trickle, not the whole population.
3. **CSV vs API duplication:** keep both or retire the CSV assessments export?
4. **`eligible_status` semantics:** the extension sets "eligible" on complete;
   confirm the API exposes a truer status we should map instead.
5. **`eligible_services` shape:** list of strings vs objects (`{name/code}`) —
   `derive_client_level` / `catalog.upsert_programs` must handle whatever the API
   returns (extension normalizes to cleaned strings).
6. **Level flip-flop:** `derive_client_level` only *sets* on a detected marker
   (never wipes) — confirm that's still the desired behavior for nightly runs.

## Decisions needed before Phase 1
- Confirm the auth approach from the Phase-0 spike.
- Orchestration host: extend `DailyPull` (recommended) vs. the export pipeline.
- Keep or retire the CSV `assessments` export.
- Should **screenings** also drive client Level 1/2, or is Level assessment-only?
- Should we also enrich screening `eligible_services` from the same API step
  (and/or add it to the extension's `buildScreeningPayloads` for parity)?

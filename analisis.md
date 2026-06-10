# Extension Data Analysis — Capture, API & CRM Field Mapping

> Synthesized from the `docs/` folder (architecture, content-scripts, sidepanel,
> data-models, django-api, etl-import) and the live source: the side panel
> `SCHEMA` (`extension/sidepanel/sidepanel.js`), the `Client`/`Address`/`Insurance`
> models (`api/models.py`), and the GoHighLevel integration
> (`api/integrations/ghl/`).

This document answers three questions:

1. **What the extension does** and how it works.
2. **Which areas** we pull information from.
3. **Which fields** we pull, mapped against (a) the fields available on the Django
   API and (b) the fields pushed to the external CRM (GoHighLevel).

---

## 1. What the extension does

CareCircle is a **Chrome Extension (Manifest V3) + Django REST API** that bridges
[Unite Us](https://uniteus.com/) (a social-care coordination platform) with the
[GoHighLevel](https://www.gohighlevel.com/) CRM for **Met Council – SCN – PHS** care
coordinators.

A coordinator works a client entirely inside the Unite Us tab. The extension:

- **Captures** client, address, insurance, case, screening, and eligibility data
  from the live Unite Us facesheet.
- **Compares** that captured data against what already exists in the Django
  backend (the "Captured-vs-CRM" view).
- **Saves / upserts** it to the backend, which then (best-effort) mirrors the
  client to the GoHighLevel CRM as a Contact.
- **Pre-fills** embedded enrollment forms (E-Form / N-Form / V-Form) with the
  client's member ID and, now, the E-Form intake fields.

### How it works (capture → validate → compare → save)

1. The coordinator opens `https://app.uniteus.io/facesheet/<client_id>`.
2. `background.js` (service worker) enables the side panel for the tab.
3. **`uw_netcapture.js`** runs in the page's **MAIN world** at `document_start`,
   wraps `fetch`/`XMLHttpRequest`, and emits the page's Unite Us auth headers
   (`Authorization`, `x-employee-id`, `x-provider-id`) via `postMessage`. It is
   read-only — it never alters or blocks requests.
4. **`uniteus.js`** (the ~3,500-line scraper) receives those credentials and:
   - parses IDs from the URL,
   - scrapes the DOM (profile, insurance cards, tables),
   - calls the Unite Us **core API** directly to enrich demographics, insurance,
     care coordinator, consent, and languages (API values win over the DOM), and
   - runs a **resumable auto-walk crawler** over the Screenings / Eligibility /
     Cases tabs, filtered to `SCREENING_ORG = "Met Council - SCN - PHS"`.
5. Everything is merged into a per-client accumulator and written to
   `chrome.storage.local` as `uw_context` (+ `uw_accum`, `uw_screenings`,
   `uw_eligibility`, `uw_cases`).
6. **`sidepanel.js`** reads `uw_context`, calls `GET /api/clients/<uuid>/` to see
   whether the client already exists, and renders the schema-driven comparison.
7. The coordinator clicks **Save**, which upserts to the backend
   (`POST /api/clients/`, `/api/screenings/bulk/`, `/api/eligibility/bulk/`,
   `/api/cases/bulk/`; the E-Form `PATCH`es `/api/clients/<uuid>/`).
8. On every client save the backend best-effort syncs the client to GoHighLevel
   (`api.integrations.ghl.sync_client`).
9. **`formfill.js`** fills the "Enrollment Platform Member ID" field inside the
   embedded form iframes.

Two gates protect the flow: a **required-fields gate** (`client_id`,
`client_name`, `client_dob`) and a **consent gate** (`consent_status` must match
`/accept/i`) before any save is allowed.

---

## 2. Areas we pull information from

| # | Source area | Mechanism | What we get |
|---|---|---|---|
| 1 | **Page URL** | `parseIdsFromUrl()` | `client_id`, `case_id`, `screening_id` UUIDs. |
| 2 | **Unite Us page DOM** | `harvestProfile()`, `harvestInsurance()`, `harvestFields()`, `harvestTableRecords()` | Profile demographics, contact info, household, consent, insurance cards, case/screening/eligibility table rows. |
| 3 | **Unite Us core API** (`core.uniteus.io`) | `enrichCapturedFromApi()` using captured auth headers | Authoritative demographics, primary address, insurance (incl. Medicaid signal), care coordinator, consent, preferred languages. **API wins over DOM.** |
| 4 | **Facesheet sub-tabs (auto-walk)** | Resumable crawler over Screenings / Eligibility / Cases | Per-record detail + question/answer pairs, filtered to *Met Council – SCN – PHS*. |
| 5 | **Embedded enrollment form (E-Form)** | Side-panel form, prefilled + agent-entered | Lead source, family/household, attestation, communication preferences, delivery address, call tracking, agent code. *(Entered, not scraped.)* |

> Credentials themselves are captured by `uw_netcapture.js` but are never stored
> with client data — they only authorize the core-API enrichment calls.

---

## 3. Field mapping — Pulled → API → External CRM

Legend for the **CRM (GoHighLevel)** column:

- A GHL field name (e.g. `firstName`) means it is pushed on every client sync.
- **`—`** means the field is stored in our API but **not** mapped to the CRM yet.
- *(custom)* marks a GHL contact **custom field**; everything else is a standard
  GHL contact property.

Sources: pulled fields = side-panel `SCHEMA`; API fields = `api/models.py`; CRM
fields = `api/integrations/ghl/contacts.py` + `custom_fields.py`.

### 3.1 Client

| Field we pull (Unite Us) | API field (`Client`) | External CRM (GHL) |
|---|---|---|
| First Name | `first_name` | `firstName` (+ `name`) |
| Middle Initial | `middle_name` | — |
| Last Name | `last_name` | `lastName` (+ `name`) |
| Suffix | `suffix` | — |
| Date of Birth | `date_of_birth` | `dateOfBirth` |
| Gender | `gender` | `gender` (only `male`/`female` forwarded) |
| Sexuality | `sexuality` | — |
| Race | `race` | — |
| Ethnicity | `ethnicity` | — |
| Marital Status | `marital_status` | — |
| Citizenship | `citizenship` | — |
| Phone | `client_phone_number` | `phone` |
| Phone Type | `phone_type` | — |
| Email | `client_email_address` | `email` |
| Spoken Language | `preferred_spoken_language` | — |
| Written Language | `preferred_written_language` | — |
| Contact Method | `preferred_communication_method` | — |
| Lead Source | `lead_source` | — |
| Enrollment From | `enrollment_from` | — |
| Consent | `consent_status` | — |
| Consent Received | `consented_at` | — |
| Eligible For | `eligible_for` (list) | — |
| Referred For | `referred_for` (list) | — |
| Is Family | `is_family` | — |
| Family Members | `total_family_members` | `total_household_members` *(custom)* |
| Monthly Income | `gross_monthly_income` | — |
| Household Size | `household_size` | `total_household_members` *(custom, fallback)* |
| Adults in Household | `adults_in_household` | — |
| Children in Household | `children_in_household` | — |
| Care Coordinator | `care_coordinator` | — |
| Agent Code | `agent_code` | — |
| Client ID (from URL) | `client_id` (PK) | `enrollment_client_id` *(custom)* |
| *(written back from GHL)* | `crm_contact_id` | GHL contact `id` |

### 3.2 Address (primary / delivery)

| Field we pull | API field (`Address`) | External CRM (GHL) |
|---|---|---|
| Type | `address_type` | — |
| Street | `line1` | `address1` (line1 + line2 joined) |
| Street 2 | `line2` | `address1` (combined) |
| City | `city` | `city` |
| County | `county` | — |
| State | `state` | `state` |
| ZIP | `postal_code` | `postalCode` |
| *(constant)* | — | `country` = `"US"` |

### 3.3 Insurance & Social Care Coverage

Both sections persist to the single `Insurance` table; the side panel routes a
stored plan to the matching section by plan name. The capture key (`capKey`)
differs from the API field name where noted.

| Field we pull (`capKey`) | API field (`Insurance`) | External CRM (GHL) |
|---|---|---|
| Plan Name (`plan_name`) | `plan_name` | — |
| Member ID (`member_id`) | `external_member_id` | — |
| Group ID (`group_id`) | `external_group_id` | — |
| Start Date (`start_date`) | `enrolled_at` | — |
| End Date (`end_date`) | `expired_at` | — |
| Status (`status`) | `status` | — |

### 3.4 Cases

| Field we pull | API field (`Case`) | External CRM (GHL) |
|---|---|---|
| Service Type | `service_type` | — |
| Service Subtype | `service_subtype` | — |
| Status | `case_status` | — |
| Provider | `provider_name` | — |
| Program | `program_name` | — |
| Network | `network_name` | — |
| Worker | `primary_worker_name` | — |
| Created | `created_at` | — |
| Updated | `updated_at` | — |

### 3.5 Screenings

| Field we pull | API field (`Screening`) | External CRM (GHL) |
|---|---|---|
| Type | `screen_type` | — |
| Status | `screen_status` | — |
| Provider | `provider_name` | — |
| Language | `language` | — |
| Consent | `consent` | — |
| Created | `screen_created_at` | — |

### 3.6 Eligibility

| Field we pull | API field (`Eligibility`) | External CRM (GHL) |
|---|---|---|
| Type | `screen_type` | — |
| Status | `screen_status` | — |
| Eligible | `eligible_status` | — |
| Provider | `provider_name` | — |
| Created | `screen_created_at` | — |

### 3.7 E-Form intake (agent-entered, not scraped)

These are entered/confirmed in the side-panel E-Form and `PATCH`ed to
`Client`. They are *not* pulled from Unite Us (some are prefilled from captured
data — e.g. family count from eligibility, delivery address from the primary
address, agent code from the saved session).

| E-Form field | API field (`Client`) | External CRM (GHL) |
|---|---|---|
| Lead Source | `lead_source` | — |
| Is this a family? | `is_family` | — |
| Total Family Members | `total_family_members` | `total_household_members` *(custom)* |
| Attestation Needed? | `attestation_needed` | — |
| Preferred Communication Channel | `communication_channels` (list) | — |
| Preferred Communication Time of Day | `preferred_communication_times` (list) | — |
| Preferred Communication Language | `preferred_languages` (list) | — |
| Delivery Address | `Address` (type `delivery`) + `different_delivery_address` | `address1`/`city`/`state`/`postalCode` (via primary-address rule) |
| Phone Call Duration | `call_duration_minutes` | — |
| Call Transfer Answered? | `call_transfer_answered` | — |
| Agent Code | `agent_code` | — |

---

## 4. Key takeaways

- **Capture coverage is broad** — ~30 client fields plus address, insurance,
  social care coverage, cases, screenings, and eligibility records, with the
  Unite Us core API as the authoritative source over the DOM scrape.
- **The API stores everything we pull.** Every pulled field maps to a model field
  in `api/models.py`; there are no captured fields without a home in the schema.
- **The external CRM mapping is intentionally thin.** GoHighLevel only receives
  the **Client as a Contact** — standard contact properties (`firstName`,
  `lastName`, `name`, `email`, `phone`, `dateOfBirth`, `gender`, address fields,
  `country`, `source`) plus **two custom fields** (`enrollment_client_id`,
  `total_household_members`). Cases, screenings, eligibility, insurance, and most
  intake fields are **not** synced to GHL.
- **To push more fields to GHL**, add `(field_id, resolver)` pairs to
  `CONTACT_FIELD_RESOLVERS` in `api/integrations/ghl/custom_fields.py` using the
  custom-field ids from `python manage.py ghl_fields`.

# Unite Us — Endpoints & Field Mappings

This documents every Unite Us field the integration **reads** or **writes**,
across all three ingestion paths, sourced directly from the code:

1. **Live core API pull** (server-side daily/on-demand sync)
   - Endpoints: `api/integrations/uniteus/api.py`
   - Field mapping (JSON:API → our serializers): `api/integrations/uniteus/mappers.py`
   - Category resolution + pull orchestration: `api/services/uniteus_import.py`
2. **CSV export import** (bulk report files from app.uniteus.io/exports)
   - Field mapping (CSV row → our serializers): `api/services/csv_import.py`
3. **Browser extension capture** (agent scrapes the Unite Us UI / calls the
   core API from the page, then POSTs to our CRM)
   - Capture: `extension/content/uniteus.js`
   - Payload build + upsert: `extension/sidepanel/sidepanel.js`

The **person/client**, **case**, **insurance**, and **notes** resources below
describe the live core-API pull. **Screenings**, **assessments**, and the
**client profile** are captured by BOTH the CSV import and the extension — see
[Screenings](#screenings), [Assessments](#assessments), and
[Client profile (extension capture)](#client-profile-extension-capture).

> Scope note: This reflects the fields **our code actually uses**, not the full
> Unite Us API specification. A resource may expose more fields than are listed
> here. Items marked **(unverified)** are open questions not yet confirmed
> against a live payload.

## Conventions

- **Base URL**: `<UNITEUS_API_BASE>/v1` (e.g. `https://core.uniteus.io/v1`).
- **Shape**: JSON:API — records are `{ "data": { "id", "type", "attributes",
  "relationships" }, "included": [...], "meta": {...} }`.
- **Auth headers** (per credential): `Authorization: Bearer <access_token>`,
  `x-employee-id`, `x-provider-id`.
- **Pagination**: `page[number]`, `page[size]`; `meta.page.total_pages` drives
  the loop (`_paginate`).
- **Filters**: `filter[<key>]=<value>` (JSON:API filter syntax).

---

## Endpoint index

| Method | Path | Client method | Purpose |
| --- | --- | --- | --- |
| GET | `/people/{id}?include=addresses` | `get_person` | Person profile + addresses |
| GET | `/consents/{id}` | `get_consent` | Consent record |
| GET | `/record_languages?filter[record_id]&filter[record_type]=Person` | `list_record_languages` | Person languages |
| GET | `/insurances?filter[person]&filter[state]&filter[plan.plan_type]` | `list_insurances` | Medical / social coverage records |
| GET | `/plans?filter[id]` | `get_plans` | Plan name + plan_type lookup |
| GET | `/cases?filter[person]&filter[state]&filter[internal_state]&filter[include_pathways]=false&sort=updated_at` | `list_cases` | A person's cases |
| GET | `/service_authorizations/{id}` | `get_service_authorization` | Single authorization |
| GET | `/service_authorizations?filter[case]` | `list_service_authorizations` | Case authorizations |
| GET | `/provided_services?filter[case]` | `list_provided_services` | Contracted / provided services |
| GET | `/invoices/{id}` | `get_invoice` | Single invoice |
| GET | `/notes?filter[subject]&filter[subject.type]` | `list_notes` | Notes for a person / case |
| GET | `/exports?filter[requester.provider]&filter[export_type]` | `list_exports` | List bulk exports |
| POST | `/exports` | `request_export` | Request a new export |
| GET | `/exports/{id}` | `get_export` | Poll one export's state |
| GET | `/file_uploads?filter[record]&filter[record.type]=export` | `list_export_file_uploads` | Export download file record |
| GET | `<file path>` | `download_export_file` | Stream a completed export CSV |
| GET | `/{resource}/{id}` | `get_resource` | Generic lookup (services, programs, networks, employees, providers) |

---

## person  (`/people/{id}`)

Mapped by `map_person_to_client` → `ClientSerializer`.

**attributes**

| Field | Type | Used as |
| --- | --- | --- |
| `first_name` | string | `first_name` |
| `last_name` | string | `last_name` |
| `date_of_birth` | date | `date_of_birth` |
| `gender` | string | `gender` |
| `marital_status` | string | `marital_status` |
| `race` | string | `race` |
| `ethnicity` | string | `ethnicity` |
| `sexuality` | string[] | joined → `sexuality` |
| `phone_numbers[]` | list | `{ phone_number, phone_type, is_primary }` → primary phone |
| `email_addresses[]` | list | `{ email_address, is_primary }` → primary email |
| `created_at` | datetime | `created_at` (source "Created" date) |
| `updated_at` | datetime | `updated_at` |

**relationships**: `person` (referenced from case)

**included → address** (`type: "address"`, via `?include=addresses`)

| Field | Type | Used as |
| --- | --- | --- |
| `address_type` | string | `type` (current/…); defaults to current |
| `line_1`, `line_2` | string | joined → `street` |
| `city` | string | `city` |
| `state` | string | `state` (truncated to 2 chars) |
| `postal_code` | string | `zip` |

---

## consent  (`/consents/{id}`)

Mapped by `map_consent`.

| Field | Type | Used as |
| --- | --- | --- |
| `state` | string | `consent_status` (+ `consent_accepted` = state == "accepted") |
| `consented_at` | datetime | `consented_at` |

---

## record_languages  (`/record_languages`)

Filtered by `filter[record_id]=<person>` and `filter[record_type]=Person`.
Merged into the client as a `languages` dict (spoken/written language fields).

---

## insurance  (`/insurances`)  — medical & social

Mapped by `map_insurance_record` → `InsuranceSerializer`, and
`map_coverage_record` → `SocialCareCoverageSerializer` (social plan_type).

**attributes**

| Field | Type | Used as |
| --- | --- | --- |
| `external_member_id` | string | `external_member_id` |
| `external_group_id` | string | `external_group_id` |
| `enrolled_at` | datetime | `enrolled_at` |
| `expired_at` | datetime | `expired_at` |
| `insurance_status` | string | `status` (active/pending/inactive; enrolled/non_enrolled for social) |
| `state` | string | fallback for `status` |

**relationships**: `plan` → resolved via `/plans`.

Query filters used: `filter[state]=active,pending,inactive`,
`filter[plan.plan_type]=commercial,medicare,medicaid,tricare` (medical) /
`social` / `medicaid`.

---

## plan  (`/plans`)

Lookup only.

| Field | Type | Used as |
| --- | --- | --- |
| `name` | string | `plan_name` |
| `plan_type` | string | `plan_type` (medicaid detection + classification) |

---

## case  (`/cases`)

Mapped by `map_case` → `CaseSerializer`.

**attributes**

| Field | Type | Used as |
| --- | --- | --- |
| `state` | string | (info only — NOT used for status; Unite Us leaves "managed" even when closed) |
| `description` | string | `case_description` |
| `opened_date` | datetime | fallback for `date_opened` |
| `closed_date` | datetime | `case_closed_at`; presence drives `case_status` = Closed |
| `created_at` | datetime | `date_opened` (preferred; has time-of-day) |
| `updated_at` | datetime | `updated_at` |

**relationships** (each resolved to a name via `get_resource`)

| Relationship | Resolved via | Used as |
| --- | --- | --- |
| `person` | — | `client_id` / `subject_id` |
| `service` | `/services` → `name` | `service_type` (the specific service = CSV **service_subtype**) |
| `service` → **`parent`** | `/services/{id}` → `parent` → `name` | `service_category` (the broad category = CSV **service_type**, e.g. `Food Assistance`) |
| `program` | `/programs` → `name` | `program_name`, `program_id` |
| `network` | `/networks` → `name` | `network_name`, `network_id` |
| `primary_worker` | `/employees` → `full_name`/`name` | `primary_worker_name`, `primary_worker_id` |
| `provider` | `/providers` → `name` | `provider_name`, `provider_id` (managing org) |
| `service_authorization` | `/service_authorizations/{id}` | see below |

Query filters used: `filter[state]=managed,off_platform`,
`filter[internal_state]=managed,pending_authorization`,
`filter[include_pathways]=false`, `sort=updated_at&sort_direction=desc`.

> **Category (`service_category`) IS captured (live API).** The broad CSV
> `service_type` category is the `/services/{id}` node's **`parent`**
> relationship. `uniteus_import._service_category` resolves the parent's `name`
> (e.g. the meal/box subtypes both roll up to `Food Assistance`, code
> `UU-FOOD`) and stores it as `service_category`. The service's OWN `name` is
> the specific service, stored as `service_type`.

---

## service_authorization  (`/service_authorizations`)

Read within `map_case` (case's auth) and standalone.

| Field | Type | Used as |
| --- | --- | --- |
| `state` | string | `service_authorization_status` (mapped: accepted→approved, requested/deferred→pending, rejected→denied) + raw label |
| `short_id` | string | `unite_us_authorization_id` |
| `approved_cents` | int | `authorized_amount` (÷100) |
| `requested_cents` | int | `service_authorization_requested_amount` (÷100) |
| `approved_unit_amount` | int | `authorized_units` |
| `requested_unit_amount` | int | fallback `authorized_units` |
| `approved_starts_at` | datetime | `service_authorization_approval_starts_at` |
| `approved_ends_at` | datetime | `service_authorization_approval_ends_at` |
| `requested_starts_at` | datetime | `service_authorization_request_starts_at` |
| `requested_ends_at` | datetime | `service_authorization_request_ends_at` |

---

## provided_service  (`/provided_services`)

Mapped by `map_provided_service` → `ContractedServiceSerializer`.

**attributes**

| Field | Type | Used as |
| --- | --- | --- |
| `state` | string | `status` |
| `unit_amount` | int | `authorized_units` |
| `starts_at` | datetime | `service_starts_at` |
| `ends_at` | datetime | `service_ends_at` |
| `service_duration` | int | (available; contracted-service duration) |
| `metadata[]` | list | `{ field, value }` — e.g. `specific_support_provided` → description |
| `created_at` | datetime | `created_at` |
| `updated_at` | datetime | `updated_at` |

**relationships**: `program`, `invoices` (list → latest invoice fetched).

---

## invoice  (`/invoices/{id}`)

Read within `map_provided_service`.

| Field | Type | Used as |
| --- | --- | --- |
| `short_id` / `invoice_number` | string | `invoice_number` |
| `invoice_status` / `state` | string | `invoice_status` |
| `total_amount_invoiced` | int | `invoice_amount` (÷100) |
| `amount_paid` | int | fallback `invoice_amount` |
| `created_at` / `approved_at` | datetime | `invoiced_at` |
| `fee_schedule_program_name` | string | `fee_schedule_program_name` |
| `fee_schedule_program_unit` | string | `unit_type` |

---

## note  (`/notes`)

Mapped by `map_note` → `Note`. Filter: `filter[subject]=<id>` +
`filter[subject.type]=person|case|referral`.

| Field | Type | Used as |
| --- | --- | --- |
| `id` | string | `source_note_id` |
| `author_name` / `created_by_name` | string | `author_name` |
| `text` | string | `body` |
| `created_at` | datetime | `source_created_at` |

**relationships**: subject (`person` / `case`).

---

## Related resources (via `get_resource`)

Only `name` (or `full_name`) is read from each:

| Resource | Fields read |
| --- | --- |
| `/services/{id}` | `name` (the specific service → `service_type`). Its **`parent`** relationship is resolved to the broad category `name` (e.g. `Food Assistance` / `UU-FOOD`) → `service_category`. Also exposes `code`, `taxonomy`, `sensitive` (not read). |
| `/programs/{id}` | `name` |
| `/networks/{id}` | `name` |
| `/employees/{id}` | `full_name`, `name` |
| `/providers/{id}` | `name` |

---

## Exports (bulk report files)

Backs the app.uniteus.io/exports page.

### export  (`/exports`)

**Export types** (`EXPORT_TYPES`): `assessments`, `screenings`, `screeningsv2`,
`cases`, `clients`, `referrals`, `users`, `notes`, `assistance_requests`,
`assistance_requests_supplemental_responses`, `invoices`, `resource_list_shares`.

**attributes**

| Field | Type | Notes |
| --- | --- | --- |
| `export_type` | string | one of the types above |
| `state` | string | `requested` → … → `completed` (poll this) |
| `details` | object | `{ start_date, end_date }` (YYYY-MM-DD) on request |

**relationships**: `requester` → `employee` (required on POST).

Filters: `filter[requester.provider]=<pid>`, `filter[export_type]=<csv>`.

### file_uploads  (`/file_uploads`)

Filter: `filter[record]=<export_id>` + `filter[record.type]=export`.

| Field | Type | Notes |
| --- | --- | --- |
| `path` | string | ActiveStorage signed redirect URL (expires ~30 min) |
| upload `state` | string | ready when the file is generated |

**relationships**: `record` → the export.

---

## CSV export ↔ API field cross-reference (cases)

The nightly CSV export and the live API populate the **same** model fields; the
mappers are intentionally aligned (`csv_import.map_case_row` ↔
`mappers.map_case`):

| Model field | CSV column | API source |
| --- | --- | --- |
| `service_type` | `service_subtype` | `/services/{id}` `name` |
| `service_category` (broad category) | `service_type` | `service` relationship → `/services/{id}` → **`parent`** → `name` |
| `program_name` | `program_name` | `program` relationship → `/programs` `name` |
| `date_opened` | `case_created_at` (→ `user_entered_opened_date` fallback) | `created_at` (→ `opened_date` fallback) |
| `case_closed_at` | `case_closed_at` (→ `user_entered_closed_date`) | `closed_date` |
| `provider_name` / `provider_id` | `provider_name` / `provider_id` | `provider` relationship → `/providers` |
| `network_name` / `network_id` | `network_name` / `network_id` | `network` relationship → `/networks` |
| `service_authorization_status` | `service_authorization_status` | `service_authorization` `state` |

---

## Screenings

An **enhanced screening** performed for a subject (client). Stored in the
`Screening` model (+ child `IdentifiedSocialNeed` / `VerifiedSocialNeed` rows)
via `ScreeningSerializer`. Keyed on `enhanced_screen_id` (UUID); append-only and
idempotent (re-imports skip an existing id).

Captured by **two** paths:

- **CSV import** \u2014 `csv_import.map_screening_group` collapses the denormalized
  one-row-per-answer screening export into one payload. Endpoint: Settings >
  Import (`screening` export type).
- **Extension** \u2014 `sidepanel.buildScreeningPayloads` scrapes the screening
  detail page (`content/uniteus.js` `harvestScreeningDetail`) and POSTs to
  `POST /api/screenings/bulk/`.

**Screening fields**

| Model field | CSV column | Extension source | Notes |
| --- | --- | --- | --- |
| `enhanced_screen_id` | `enhanced_screen_id` | detail submission UUID (\u2192 row id fallback) | PK |
| `subject_id` | `subject_id` | detected `client_id` | FK link to `Client` if present |
| `screen_created_at` | `screen_created_at` | list-view date (parsed \u2192 ISO) | |
| `screen_status` | `screen_status` | list-view status | free-form |
| `screen_type` | `screen_type` | list-view form name (e.g. "HM #3", "SCN") | |
| `screen_source` | `screen_source` | list-view form | |
| `provider_name` | `provider_name` | list-view submitter | |
| `performing_organization_name` | `performing_organization_name` | list-view org | |
| `duration` | `duration` (seconds) | detail minutes \u00d7 60 | PositiveInteger, seconds |
| `questions_answers` | `question_primary_text` + answer\* (deduped by `answer_id`) | detail Q/A pairs | `[{question, answer}]` JSON |
| `identified_social_needs` | `identified_social_need_name` (distinct) | screening-results chips | array of name strings |
| `eligible_status` | `eligible_status` | \u2014 | |
| `eligible_services` | `eligible_services` (list) | \u2014 | JSON list |

\* answer value resolves in order: `question_option_text`, `value_string`,
`answer_value`, then the typed `answer_value_bool/int/float/datetime`.

**Child: `IdentifiedSocialNeed`** (CSV only; `_screening_need_rows`, deduped by id)

| Model field | CSV column |
| --- | --- |
| `identified_social_need_id` | `identified_social_need_id` (PK) |
| `identified_social_need_code` | `identified_social_need_code` |
| `identified_social_need_name` | `identified_social_need_name` |
| `identified_created_at` | `identified_created_at` |
| `identified_updated_at` | `identified_updated_at` |
| `is_need_sensitive` | `is_need_sensitive` |

**Child: `VerifiedSocialNeed`** (CSV only)

| Model field | CSV column |
| --- | --- |
| `verified_social_need_id` | `verified_social_need_id` (PK) |
| `verified_social_need_code` | `verified_social_need_code` |
| `verified_social_need_name` | `verified_social_need_name` |
| `verified_created_at` | `verified_created_at` |
| `verified_updated_at` | `verified_updated_at` |

---

## Assessments

An **assessment** (formerly "Eligibility") for a subject. Stored in the
`Assessment` model via `AssessmentSerializer`. Keyed on `assessment_id` (UUID).

Captured by **two** paths:

- **CSV import** \u2014 `csv_import.map_assessment_group` collapses the denormalized
  one-row-per-question assessment export. Endpoint: Settings > Import
  (`assessments` export type). The export carries no eligibility results, so
  `eligible_status` / `eligible_services` are left empty.
- **Extension** \u2014 `sidepanel.buildEligibilityPayloads` scrapes the eligibility
  detail page (`content/uniteus.js` `harvestEligibilityDetail`) and POSTs to
  `POST /api/assessments/bulk/`.

**Assessment fields**

| Model field | CSV column | Extension source | Notes |
| --- | --- | --- | --- |
| `assessment_id` | `submission_id` | detail UUID (\u2192 row id fallback) | PK |
| `subject_id` | `client_id` | detected `client_id` | FK link to `Client` if present |
| `screen_created_at` | `submission_created_at` | list-view date (parsed \u2192 ISO) | |
| `form_name` | `form_name` | \u2014 | e.g. "Unite NYC - Food Assistance Assessment" |
| `provider_name` | `submission_created_by_name` (submitter) | list-view submitter | |
| `performing_organization_name` | `provider_name` (org) | list-view org | |
| `questions_answers` | `question` + `responses` | detail Q/A pairs | `[{question, answer}]` JSON |
| `eligible_status` | \u2014 (export has none) | "eligible" when status ~ complete | |
| `eligible_services` | \u2014 (export has none) | eligibility-results chips | JSON list; drives `Client.is_level` |
| `duration` | \u2014 | \u2014 | PositiveInteger, seconds |

> On save, `AssessmentSerializer` derives the client's service level
> (`Client.is_level`) from any "Level 1"/"Level 2" marker in `eligible_services`.

---

## Client profile (extension capture)

The extension also reads the Unite Us **person profile** directly from the page
/ core API and upserts it to `POST /api/clients/` (auto-saved as the agent walks
the tabs; see `sidepanel.buildClientPayload` + `content/uniteus.js`
`mapPersonToClient` / `mapInsuranceRecords` / `mapCareCoordinator`). This is the
same `Client` model the live-API `map_person_to_client` and the CSV
`map_client_group` populate.

**Profile fields** (extension `buildClientPayload` \u2192 `ClientSerializer`)

| Model field | Extension source (person `attributes` / page) |
| --- | --- |
| `client_id` | detected person id |
| `first_name` / `last_name` / `middle_name` / `suffix` | `first_name` / `last_name` / `middle_name` / `suffix` |
| `date_of_birth` | `date_of_birth` |
| `citizenship` | `citizenship` (titleized) |
| `race` / `ethnicity` / `gender` / `marital_status` | label-mapped enums |
| `sexuality` / `sexuality_other` | `sexuality[]` (joined) / `sexuality_other` |
| `gross_monthly_income` | `gross_monthly_income` |
| `client_phone_number` / `phone_type` | primary of `phone_numbers[]` |
| `client_email_address` | primary of `email_addresses[]` |
| `consent_status` / `consented_at` | captured consent |
| `preferred_spoken_language` / `preferred_written_language` | captured languages |
| `agent_code` | captured `care_coordinator` (page / care-team relationship) |
| `household_size` / `total_family_members` / `is_a_family` | page-entered family count |
| `lead_source` / `communication_channels` / `preferred_communication_times` / `preferred_languages` | agent-entered Profile-tab fields |
| `created_at` / `updated_at` | person `created_at` / `updated_at` |
| `addresses[]` | page address \u2192 `{type, street, city, county, state, zip}` |
| `insurances[]` | `mapInsuranceRecords` (non-social) \u2192 medical `Insurance` records |
| `social_care_coverages[]` | `mapInsuranceRecords` (social) \u2192 `SocialCareCoverage` records |

> The extension flags insurance/coverage lists authoritative
> (`reconcile_insurances` / `reconcile_social_care_coverages`) only when the
> coverage sections were actually on the page, so a partial scrape never
> deactivates stored coverage.

### Client field parity across the three paths

| Model field | Live API (`map_person_to_client`) | CSV (`map_client_group`) | Extension (`buildClientPayload`) |
| --- | --- | --- | --- |
| `first_name` / `last_name` | yes | yes | yes |
| `middle_name` | no | yes | yes |
| `suffix` | no | no | yes |
| `date_of_birth` | yes | yes | yes |
| `gender` / `marital_status` / `race` / `ethnicity` / `sexuality` | yes | yes | yes |
| `citizenship` | no | yes | yes |
| `gross_monthly_income` | no | yes | yes |
| `household_size` / `adults_in_household` / `children_in_household` | no | yes | `household_size` / family only |
| `client_phone_number` / `phone_type` / `client_email_address` | yes | yes | yes |
| `consent_status` / `consent_accepted` / `consented_at` | via `/consents` | yes | yes |
| `preferred_spoken_language` / `preferred_written_language` | via `/record_languages` | yes | yes |
| `care_coordinator` / `agent_code` | no | `care_coordinator` | `agent_code` |
| `created_at` / `updated_at` | yes | yes | yes |
| `addresses[]` (incl. `county`) | yes (no `county`) | yes | yes |
| `insurances[]` / `social_care_coverages[]` | yes | yes | yes |

> **Parity gaps (by design, not bugs):** the live-API person pull does not
> return `middle_name`, `citizenship`, `gross_monthly_income`, household counts,
> or `care_coordinator`; consent + languages come from separate endpoints. The
> CSV export and extension fill these. All three paths write the **same**
> `Client` model, so a field captured by any path is stored.

---

## CSV export ↔ API field cross-reference (clients)

| Model field | CSV column | API source |
| --- | --- | --- |
| `first_name` / `last_name` / `middle_name` | `first_name` / `last_name` / `middle_name` | person `attributes` (no `middle_name` on API) |
| `date_of_birth` | `date_of_birth` | `date_of_birth` |
| `client_phone_number` / `phone_type` | `client_phone_number` / `phone_type` | primary `phone_numbers[]` |
| `client_email_address` | `client_email_address` | primary `email_addresses[]` |
| `consent_status` / `consented_at` | `client_consent_status` / `client_consented_at` | `/consents` `state` / `consented_at` |
| `created_at` / `updated_at` | `client_created_at` / `client_updated_at` | person `created_at` / `updated_at` |
| `addresses[]` | `client_address_*` | `included` addresses |
| `insurances[]` | `insurance_*` (non-social `insurance_plan_type`) | `/insurances` + `/plans` |
| `social_care_coverages[]` | `insurance_*` (`insurance_plan_type` == social) | `/insurances` (social) + `/plans` |

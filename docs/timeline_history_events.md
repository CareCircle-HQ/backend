# Timeline & History Events

This document describes **when we write an event to a client's timeline / history**,
**what we store**, and — for each event — **whether we already capture "who did it"
(actor)** and **"what exactly changed" (a before → after diff)**.

The goal is a single reference for every event so we can see, at a glance, which
events are fully attributed (who + what changed) and which still have gaps.

---

## Two separate systems

There are **two** independent records. Don't confuse them.

### 1. `TimelineEvent` — the curated, human-facing event stream
- Model: `TimelineEvent` (`api/models.py`, event types in `TimelineEventType`).
- Written through one low-level writer, `emit_timeline_event(...)`, and a set of
  `event_for_*(...)` builders in `api/services/timeline.py`.
- This is what the member profile **History tab** shows: a titled, badged row per
  meaningful occurrence (consent, screening, case opened, verification completed,
  kitchen changed, member paused, …).
- **Create-once**: most builders pass a `dedupe_key`, so re-saving / re-importing
  the same entity does **not** create a second row or re-stamp the first. Each
  domain occurrence is a single, stable point on the timeline.
- Each row stores: `event_type`, `occurred_at`, `title`, `subtitle`, `badge_text`,
  `badge_tone`, **`source`**, **`actor`**, a generic link to the source entity, and
  a free-form **`metadata`** JSON blob.

### 2. django-simple-history — the field-level audit log
- Configured in `api/history.py` via `tracked_history()`; every tracked model gets a
  `Historical*` table that snapshots the **full row on every change**, plus two
  extra columns:
  - `change_source` — `import | extension | admin | crm | system`
  - `change_actor` — e.g. `agent:355`, `user:alex`, `system:unite-us-import`
- This **always** records who/what changed a row and the full before/after (by
  diffing consecutive snapshots). It is the low-level "audit everything" log.
- Attribution happens two ways:
  1. **Server jobs** wrap writes in `change_context(source, actor)`
     (`api/history.py`), which sets a thread-local.
  2. **HTTP edits** (extension / admin) are attributed lazily from the request via
     `_attribution_from_request()` — an agent principal → `(extension, agent:<code>)`,
     a Django user → `(admin, user:<name>)`.

> **Key point for the gap analysis:** simple-history *already* records "who" and
> "what changed" for every tracked model write. The **timeline** is the curated
> layer, and it's the one where the "what changed" diff is inconsistent — only a
> few timeline events carry a structured before → after diff today.

---

## What "who" and "what changed" mean here

- **Who (`actor`)** — a string identifying the principal:
  - `agent:<code>` — an extension/CRM agent with a dialer code
  - `user:<name>` — a portal agent without a code (Management / CS roles)
  - `system:<job>` — a background job (`system:unite-us-import`,
    `system:csv-import`, `system:assessment-results`)
  - `"System"` — generic fallback for lifecycle logic with no acting principal
  - `""` — **not attributed** (a gap)
- **What changed (`metadata.changes`)** — a structured list built by
  `build_change_list([(label, before, after), …])`, rendering as
  `{"field", "from", "to"}` rows the History tab can show as a clean diff.
  Most events store only *current state* or a *reason* in metadata, **not** this
  structured diff.

Actor-string helpers:
- Extension path: `_agent_actor(request)` — `api/views.py`
- Portal path: `_agent_actor(agent)` — `api/portal/views_members.py` (prefers
  `agent:<code>`, falls back to `user:<name>`)
- Lifecycle path: `author = actor_label or _actor_name(actor)` — defaults to
  `"System"` (`api/services/lifecycle.py`)

---

## The four write sources

| Source | Who triggers it | `source` value | `actor` value |
|---|---|---|---|
| **Browser extension** | Agent working a client in Unite Us (bulk upsert endpoints in `api/views.py`) | `extension` (some default to `system`/`crm`, see notes) | `agent:<code>` (or `""` if the JWT has no code) |
| **Portal** | Staff/manager actions in the web app (`api/portal/views_members.py`, `views_tickets.py`) | `crm` / `admin` / `system` depending on the builder | `agent:<code>` or `user:<name>` |
| **CSV importer** | Nightly CSV export → import (`api/services/csv_import.py`) | `import` | `system:csv-import` (simple-history: `csv:<triggered_by>`) |
| **Unite Us daily pull** | Nightly / on-demand core-API pull (`api/services/uniteus_import.py`) | `import` | `system:unite-us-import` |
| **System lifecycle / eligibility** | Reconciliation logic invoked by the importers or portal (`api/services/lifecycle.py`, `eligibility.py`) | `import` / `admin` / `system` | `"System"` unless an acting agent is threaded through |

> **Important gating:** the **CSV importer and Unite Us pull only emit timeline
> events when side effects are enabled** (manual upload / `emit_side_effects=True`).
> The routine cron refresh sets this **off** to avoid DB churn, so on a normal
> nightly run the importers mostly write **simple-history** rows, not timeline
> rows. Timeline "capture" of imported entities happens on the first side-effecting
> pass (or via the extension when an agent opens the client).

---

## Event catalog

Legend for the two right-hand columns:
- **Who?** = is an `actor` recorded on the timeline event?
  ✅ yes · ⚠️ sometimes/weakly · ❌ no
- **What changed?** = is a structured before → after `changes` diff stored?
  ✅ yes · 🟡 partial (previous/new values or a reason in metadata, not a field diff) · ❌ no

### A. Capture / domain events (entity first appears)

| Event (type) | Emitted by | Trigger | Stored (title / subtitle / metadata) | Who? | What changed? |
|---|---|---|---|---|---|
| **Consent Granted** (`consent_granted`) | Extension, CSV, Pull | Client consent becomes accepted (once) | "Consent Granted" / signer name | ✅ ext=`agent:`, import=`system:` | ❌ (one-time fact) |
| **Screening** (`screening`) | Extension, CSV | A screening is captured | screen type / org · unmet-need count badge · **metadata: `eligible_status`, `eligible_services`, `identified_social_needs`, `results_count`** | ✅ | ✅ eligibility + results (see note) |
| **Assessment** (`assessment`) | Extension, CSV, assessment-enrichment | An assessment is captured | "Assessment" / org · eligible status badge · **metadata: `eligible_status`, `eligible_services`, `form_name`, `results_count`** | ✅ | ✅ eligibility + results (see note) |

> **Screening/Assessment eligibility & results (implemented):** `event_for_screening`
> and `event_for_assessment` now record **what the member is eligible for**
> (`eligible_services`) and the **results** (screening `identified_social_needs` +
> a `results_count`; full Q&A stays on the linked entity). Because `eligible_services`
> for assessments often arrives *after* the CSV import created the row (via the
> screenings-ingestion enrichment pull), `emit_timeline_event` gained an opt-in
> `update_metadata` flag and the builders an `resync=` param; `assessment_enrichment`
> calls `event_for_assessment(..., resync=True)` to back-fill the existing
> create-once row. The History tab renders these via a dedicated "Eligible for" /
> "Identified social needs" chip block (`EligibilityResults` in `HistoryTab.tsx`).
| **Case** (`case_opened`) | Extension, CSV, Pull | A case first appears | program / type · provider · status badge · **metadata: `case_type`, `product_kind` (meals/boxes), `is_governing`, `auth_status`(+label), `auth_window_start/end`** (metadata **resyncs** to current auth state) | ✅ | ✅ classification + current auth window |
| **Insurance** (`insurance`) | CSV, Pull | Insurance record appears | plan / member id · status badge · **metadata: `plan_type`, `status`, `is_primary`, `enrolled_at`, `expired_at`, `expired`, `meets_medicaid_rule`(+`medicaid_rule_note`), `verified`** (resyncs) | ✅ import=`system:` | ✅ Medicaid rule + status + expiry |
| **Social Care Coverage** (`social_care_coverage`) | CSV, Pull | Coverage record appears | plan / member id · status badge · **metadata: `plan_type`, `status`, `enrolled_at`, `expired_at`, `expired`, `verified`** (resyncs) | ✅ | ✅ status + expiry |

### B. Case transition events (has previous → new values)

| Event (type) | Emitted by | Trigger | Metadata | Who? | What changed? |
|---|---|---|---|---|---|
| **Case Status Changed** (`case_status_changed`) | Extension, CSV, Pull (via `case_events.record_case_change`) | Case status transitions (Open→Closed/Cancelled/Managed) | `previous_status`, `new_status`, `closed_reason`, `product_kind`, `import_run` | ✅ | 🟡 prev/new values (not `changes` list, but the info is there) |
| **Case Authorization Changed** (`case_auth_changed`) | Extension, CSV, Pull (via `case_events`) | Service auth transitions (approved/denied/pending/expired) | `previous_auth`, `new_auth`, **`auth_window_start/end`, `product_kind`, `authorized_amount`**, `import_run` (window also shown in subtitle) | ✅ | 🟡 prev/new + auth window |

### C. Verification / enrollment-stage events

| Event (type) | Emitted by | Trigger | Metadata | Who? | What changed? |
|---|---|---|---|---|---|
| **Stage transitions** — Pending Validation, Validated, Verification Requested/Completed, Awaiting Kitchen, Service Activated/On Hold/Resumed/Completed/Closed/Cancelled, Verification Disregarded, Enrolled (`event_for_verification`) | System lifecycle (`advance_enrollment`); also extension & portal request-verification paths | Every guarded stage transition | `previous_stage(+label)`, `new_stage(+label)`, `trigger`, `reason`, `actor_label`, `case_id`, `program`, `kitchen` | ✅ (or `"System"`) | 🟡 rich prev/new stage + trigger + reason (not a `changes` list) |
| **Verification Renewed** (`event_for_verification_renewed`) | Extension re-request, portal | Verification re-requested | — | ✅ | ❌ |
| **Verification Case Switched** (`event_for_verification_case_switched`) | Extension set-case, portal | Governing case swapped on an enrollment | `previous_case`, `new_case` | ✅ | 🟡 prev/new case |
| **Verification Completed (submitted)** (`event_for_verification_submitted`) | Portal verification wizard | Agent completes verification | members list, delivery address, verified flags | ✅ `actor_label` | ❌ |
| **Verification Disregarded** (`verification_disregarded`, direct emit) | Portal | Agent dismisses a pending verification | reason in subtitle | ⚠️ `agent.name`; **`source="portal"`** (inconsistent literal) | ❌ |
| **Nutritionist Approved** (`nutritionist_approved`, direct emit) | System lifecycle (`approve_nutritionist`) | Nutritionist legal sign-off | signature, approved_by/at, pdf_key, members_reviewed | ⚠️ `agent.name`; **`source=""`** (not set) | ❌ |
| **Nutritionist Paused** (`nutritionist_paused`, direct emit) | Portal (`NutritionistPauseView`) | Nutritionist pauses a member | reason | ⚠️ `agent.name`; **`source=""`** (not set) | ❌ |

### D. Member lifecycle / eligibility events

| Event (type) | Emitted by | Trigger | Metadata | Who? | What changed? |
|---|---|---|---|---|---|
| **Out of Orbit** (`out_of_orbit`) | Portal (manual deactivate / auto from dietary edit), kitchen-assign flow | Member set out of orbit | `reason`, `menu_type` | ✅ | ❌ (reason only) |
| **Out of Range** (`out_of_range`) | Household add (serializer), eligibility | Member outside delivery range | `reason`, `zip` | ✅ | 🟡 reason + zip |
| **Member Ineligible** (`member_ineligible`) | System eligibility / lifecycle | Client fails eligibility (insurance / Medicaid type / address / etc.) | `reasons`, **`causes`, `reason_causes`** (each reason tagged `insurance` / `medicaid_type` / `address` / `social_coverage` / `other`) | ✅ (or `"System"`) | ✅ cause-tagged reasons |
| **Eligibility Restored** (`member_eligibility_restored`) | System eligibility | Client passes again | — | ✅ | ❌ |
| **Coverage Hold** (`member_coverage_hold`) | System eligibility | Recoverable coverage gap pauses service | `reasons` | ✅ (or `"System"`) | 🟡 reasons |
| **Coverage Restored** (`member_coverage_restored`) | System eligibility | Coverage recovers | — | ✅ | ❌ |
| **Service Inactive** (`member_service_inactive`) | System lifecycle (case closure) | Governing case closes | `case_id`, `program`, `closed_on` | ✅ (or `"System"`) | 🟡 context |
| **Service Reactivated** (`member_service_reactivated`) | System lifecycle | Service resumes | — | ✅ | ❌ |
| **Governing Case Changed** (`member_governing_case_changed`) | System lifecycle (case reconcile) | Governing internal-service case changes | `previous_case_id`, `new_case_id`, `auth_status`, `program`, `reason` | ✅ (or `"System"`) | 🟡 prev/new case |
| **Program Switched** (`member_program_switched`) | System lifecycle | Governing case switches meals↔boxes | `previous_kind`, `new_kind`, case ids, `auth_status`, `reason` | ✅ (or `"System"`) | 🟡 prev/new kind |
| **Case Mismatch** (`member_case_mismatch`) | System lifecycle | Household↔individual scope switch needs CS review | mismatch_type, case ids, household types, reason, auto_resolved | ✅ (or `"System"`) | 🟡 prev/new scope |
| **Member Reactivated** (`member_reactivated`) | Portal, kitchen-assign flow | Member returns to Active | — | ✅ | ❌ |
| **Member Paused** (`member_paused`) | Portal, system (scope/eligibility) | Member paused | `reason`, `menu_type` | ✅ (or `"System"`) | ❌ (reason only) |
| **Member Unpaused** (`member_unpaused`) | Portal, system | Member unpaused | `reason`, `menu_type` | ✅ (or `"System"`) | ❌ (reason only) |
| **Household Member Added** (`household_member_added`) | Portal (household tab + verification wizard) | Member added to household | — | ✅ | ❌ |
| **Household Member Removed** (`household_member_removed`) | Portal | Member removed | — | ✅ | ❌ |

### E. Fully-diffed edit events ✅ (the model to follow)

These already record **both** who and a precise field-level before → after diff via
`build_change_list`. They are the template for how the rest should eventually look.

| Event (type) | Emitted by | Trigger | Diffed fields | Who? | What changed? |
|---|---|---|---|---|---|
| **Delivery Address Changed** (`delivery_address_changed`) | Portal (`DeliveryAddressView`) | Agent edits delivery address | Street, Unit, City, State, ZIP, Delivery notes | ✅ | ✅ |
| **Dietary Info Updated** (`dietary_changed`) | Portal (dietary edit) | Agent edits dietary data | dietary_restrictions, food_allergies, other_dietary_restrictions, meal_category, menu_type, general_verification_notes | ✅ | ✅ |
| **Kitchen Assigned / Changed** (`kitchen_assigned` / `kitchen_changed`) | Portal (kitchen assign / edit / cadence edit) | First assignment or re-assignment | Kitchen, Cadence | ✅ | ✅ (change variant only; first-assign has nothing to diff) |
| **Product Type Changed** (`product_type_changed`) | System lifecycle | Product kind changes | `previous`, `new` | ✅ (or `"System"`) | 🟡 prev/new (not full `changes` list) |

### F. Tickets

| Event (type) | Emitted by | Trigger | Metadata | Who? | What changed? |
|---|---|---|---|---|---|
| **New Ticket Created** (`ticket_created`) | Portal (`views_tickets`), system (`services/tickets.open_ticket`) | A ticket is opened for a client | `ticket_type`, `severity`, `ticket_source` | ⚠️ portal=`agent:<code>` else `""`; system=passed-in actor | ❌ |

---

## Gap analysis — what's missing today

**"Who" (actor) is broadly covered**, but with a few soft spots:
- `event_for_ticket_created` from the portal records `""` when the agent has no
  code (should fall back to `user:<name>` like the other portal actions).
- Direct `emit_timeline_event` calls for **Nutritionist Approved / Paused** and
  **Verification Disregarded** use `agent.name` (not the normalized
  `agent:<code>` / `user:<name>` form) and set `source` to `""` or the literal
  `"portal"` instead of a `ChangeSource` value. These should be normalized.
- System lifecycle events fall back to `"System"` when no acting agent is threaded
  through — correct for cron, but where a portal action *triggers* the lifecycle
  path we could thread the real agent through so it reads `user:<name>` instead of
  `System`.

**"What changed" (structured diff) is the real gap.** Only these carry a true
field-level `changes` diff: **Delivery Address, Dietary Info, Kitchen/Cadence**.
Everything else stores either:
- previous/new *scalar* values (case status/auth, verification stage, governing
  case, program kind, product type) — the information is present but **not** in the
  standardized `changes` shape the History tab renders as a diff; or
- only current state / a reason (consent, screening, assessment, insurance,
  coverage, pause/unpause, out-of-orbit, household add/remove, tickets) — **no**
  before/after at all.

### Suggested direction (not yet implemented)
1. **Backfill actor normalization** on the three direct-emit events + portal ticket
   creation so every timeline row has a consistent `agent:` / `user:` / `system:`
   actor.
2. **Promote the "prev/new in metadata" events to a real `changes` list** using
   `build_change_list`, so Case Status, Case Authorization, Verification Stage,
   Governing Case, Program Switched, and Product Type render as proper diffs.
3. **Decide per event whether a diff is even meaningful** — capture/one-time facts
   (consent, screening, assessment, ticket-created) probably don't need a diff; the
   value there is the actor + occurrence, which they already have.

---

## Where the code lives

- Timeline writer + all builders: `api/services/timeline.py`
  (`emit_timeline_event`, `build_change_list`, `event_for_*`)
- Timeline model + event types: `api/models.py` (`TimelineEvent`, `TimelineEventType`)
- Audit log (simple-history) + attribution: `api/history.py`
  (`tracked_history`, `change_context`, `_attribution_from_request`)
- Extension write path: `api/views.py` (`_agent_actor`, the bulk `*ViewSet`s)
- Portal write path: `api/portal/views_members.py`, `api/portal/views_tickets.py`
- CSV importer: `api/services/csv_import.py`
- Unite Us daily pull: `api/services/uniteus_import.py`
- Shared case transitions: `api/services/case_events.py`
- Lifecycle / eligibility (system-emitted): `api/services/lifecycle.py`,
  `api/services/eligibility.py`

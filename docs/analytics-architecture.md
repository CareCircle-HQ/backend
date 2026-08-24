# Analytics Architecture — Phases 0–2

Make dashboards and the new Administration → **Data** page fast as the dataset
grows, without leaving the Django/Postgres stack. Separate the operational
(OLTP) workload from analytics (OLAP): keep Postgres as the source of truth, and
serve analytics from indexed/pre-computed read models on a replica.

## Decisions (locked)

- **Read-model grain: one row per enrollment** (`EnrollmentVerification`).
- **Freshness SLA: 1 hour** (analytics may lag the operational DB by up to ~1h).
- **Invalidation: scheduled rebuild** (see "Refresh strategy") — not ORM signals.
- **Primary consumer: the data team**, who filter on almost any field and
  produce **CSV exports** from the Data page.

## Current state (baseline)

- The Data page (`DataListView`) is today an exact clone of the Members list
  (`MembersListView`) — a live, multi-join, household-grouped query. Fine as a
  placeholder; it will be re-pointed at the read model in Phase 1.
- Enrollment count today ≈ 20k (`EnrollmentVerification`) — small, so a full
  hourly rebuild is trivial now; the design still scales to millions.

---

## Phase 0 — Tune Postgres in place

Cheap, immediate wins for the **live** Members list + dashboards (the Data page
gets its speed from the Phase 1 read model, so don't over-index live tables for
Data-only filters).

1. **Profile** `/members/`, `/dashboard/`, `/cs-dashboard/` with
   `EXPLAIN (ANALYZE, BUFFERS)`; fix pathological patterns (OR across two
   multi-valued joins + `DISTINCT` — see the Kitchen-Assignment dashboard fix).
2. **Add indexes** that the live Members list actually filters/sorts on:
   - `EnrollmentVerification.kitchen` (FK, currently unindexed) — filtered.
   - `Client.created_at` (only present inside a composite today) — sorted/filtered.
   - `Insurance.expired_at`, `SocialCareCoverage.expired_at` — expiration filters.
   - `DeliveryOrder.delivered_at` — last-delivered lookups.
   - `Screening.screen_created_at`, `Assessment.screen_created_at` — date filters.
   - Partial indexes for hot predicates (e.g. `status='active'`,
     `stage='pending_verification'`).
3. **Partition** the big append-only tables by date (range/BRIN): stage events,
   history, `DeliveryOrder`, `DeliveryOrderProof` — so the planner prunes.

---

## Phase 1 — `EnrollmentAnalytics` read model (the core)

A **real Django table** (not a materialized view), one row per enrollment, with
every Data-page filter flattened into an indexed column. A real table is
required because the Data page filters on *arbitrary* combinations and needs
**GIN indexes on array columns** (multi-selects) and normal Django migrations —
neither of which a materialized view does well.

### Filter → source → read-model column → index

| Data-page filter | Source (model.field) | Read-model column | Index |
| --- | --- | --- | --- |
| Age (range) | `Client.date_of_birth` | `dob date` | btree (filter DOB range from age bounds) |
| Member created | `Client.created_at` | `member_created_at timestamptz` | btree/BRIN |
| Care coordinator | `Client.care_coordinator` (free text) | `care_coordinator text` | btree |
| Primary care coordinator | ⚠ not a field — closest is `Case.primary_worker_name` | `primary_care_coordinator text` | btree |
| Cadence | `EnrollmentVerification.delivery_weekdays` (JSON) / `MemberDeliverySchedule.delivery_days_cadence` | `cadence text` | btree |
| Kitchen | `EnrollmentVerification.kitchen` | `kitchen_id uuid`, `kitchen_name text` | btree |
| Menu type | `MemberDietaryProfile.menu_type` | `menu_type text` | btree |
| Delivery status (current) | DERIVED — latest `DeliveryOrder.status` | `current_delivery_status text` | btree |
| Delivery status (last PO) | DERIVED — latest `PurchaseOrder.delivery_status` | `last_po_delivery_status text` | btree |
| Last date delivered | DERIVED — latest `DeliveryOrder.delivered_at` | `last_delivered_at timestamptz` | btree |
| Allergies | `MemberDietaryProfile.food_allergies` (JSON list) | `allergies text[]` | **GIN** |
| Medical conditions | `MemberDietaryProfile.conditions` (JSON list) | `medical_conditions text[]` | **GIN** |
| Medications | `MemberDietaryProfile.medications` (JSON list) | `medications text[]` | **GIN** |
| Insurance status | `Insurance.status` | `insurance_status text` | btree |
| Insurance expiration | `Insurance.expired_at` | `insurance_expires_at timestamptz` | btree |
| Social coverage status | `SocialCareCoverage.status` | `social_status text` | btree |
| Social coverage expiration | `SocialCareCoverage.expired_at` | `social_expires_at timestamptz` | btree |
| Attestation status (needed/completed) | `Client.attestation_needed` (bool) | `attestation_status text` | btree |
| Attestation requested date | ⚠ not in DB (GHL CRM only) | `attestation_requested_at timestamptz` (nullable, backfill later) | btree |
| Attestation completed date | ⚠ not in DB (GHL CRM only) | `attestation_completed_at timestamptz` (nullable, backfill later) | btree |
| Has screening + date | DERIVED `EXISTS(Screening)` / `Screening.screen_created_at` | `has_screening bool`, `screening_at timestamptz` | btree |
| Has eligibility assessment + date | DERIVED `EXISTS(Assessment)` / `Assessment.screen_created_at` | `has_eligibility_assessment bool`, `eligibility_assessment_at timestamptz` | btree |
| Eligible for (multi-select services) | `Assessment.eligible_services` (JSON list) | `eligible_services text[]` | **GIN** |
| (case filters) | `Case.case_type/case_status/service_authorization_status/date_opened/program` | mirror onto the row: `case_type`, `case_status`, `auth_status`, `case_opened_at`, `program_name` | btree |

Plus identity/join columns: `enrollment_id (pk/fk)`, `client_id`, `household_id`,
`case_id`, `stage`, `is_primary`, and a `refreshed_at` watermark.

### Multi-valued fields
Store as Postgres `ArrayField(text)` with a **GIN index**, so a multi-select
filter is a fast `@>` / `&&` containment test (`allergies && ['peanuts','fish']`).
Source data is JSON lists today — the refresh normalizes them into the array
columns.

### Gaps to flag before those filters can work
- **Primary Care Coordinator**: no dedicated field. Decide: use
  `Case.primary_worker_name`, or add a real field to `Client`/enrollment.
- **Attestation requested/completed dates**: only exist in GHL CRM custom
  fields, not the DB. Need a small ingestion/backfill to populate them before
  the date filters are meaningful (columns can exist nullable meanwhile).
- **Menu type / dietary data** live on `MemberDietaryProfile` (one per household
  member per enrollment) — the read model resolves the profile for the
  enrollment's client.

### Refresh strategy (invalidation) — my recommendation

Given the **1-hour SLA** and current ~20k enrollments, use **scheduled rebuilds
on Celery beat**, not ORM signals:

- **Now (MVP): hourly FULL rebuild.** Recompute every enrollment row in one
  batched pass (bulk `select_related`/`prefetch_related` + `bulk_create`/upsert).
  At 20k rows this is seconds, dead simple, and **cannot drift**.
- **Later (scale): watermark INCREMENTAL.** Keep a `last_run_at`; each hourly run
  recomputes only enrollments whose enrollment/client/case/insurance/…/delivery
  source rows have `updated_at > last_run_at`. Add a **nightly full reconcile** to
  heal any missed edges. Scales to millions without touching unchanged rows.

Why not ORM signals / CDC: signals are scattered across many models (easy to
miss a source, e.g. a dietary-profile or delivery change) and fragile under bulk
updates; CDC is real-time infra you don't need for a 1-hour SLA. The watermark
approach gets the same freshness with one scheduled job and a nightly safety net.

### Wiring
Re-point `DataListView.get_queryset()` at `EnrollmentAnalytics`; the Data-page
serializer/filter layer maps each query param to a column (btree) or array
containment (GIN). The CSV export streams from the same table. The Members list
(`MembersListView`) is untouched — it stays on the live graph, tuned in Phase 0.

---

## Phase 2 — Read replica

- Provision a streaming read replica of Postgres.
- Add a Django **DB router**: route read-only analytics (Data page + its export,
  dashboards) to the **replica**; all writes and read-your-writes operational
  screens to the **primary**.
- Gotchas:
  - **Replication lag** — keep operational screens (verification, member detail
    right after an edit) on the primary.
  - The `EnrollmentAnalytics` **refresh job must read the primary** (or be
    lag-aware) so it never computes from stale replica data.

---

## Rollout order

1. Phase 0 indexes + partitioning + query fixes (immediate live-page wins).
2. `EnrollmentAnalytics` model + migration (columns + btree + GIN indexes).
3. Hourly full-rebuild Celery task (+ nightly reconcile); backfill once.
4. Re-point `DataListView` + Data export at the read model; wire the filter set.
5. Read replica + DB router; move Data page + dashboards onto it.
6. Close the gaps (primary care coordinator field, attestation-date ingestion).
7. (Only if Postgres later can't keep up) Phase 3: ship `EnrollmentAnalytics` to
   a columnar store (ClickHouse / Timescale-Citus / warehouse) via ETL/CDC.

## Testing

- Correctness: assert read-model rows match the live query for a sample of
  enrollments across each filter (esp. the derived + multi-valued fields).
- Freshness: assert `refreshed_at` advances; reconcile heals an injected drift.
- Performance: `EXPLAIN ANALYZE` each filter hits its index (btree/GIN), not a
  seq scan; measure the Data page + export at prod-scale row counts.

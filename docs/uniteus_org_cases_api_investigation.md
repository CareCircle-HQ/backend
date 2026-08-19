# Investigation: Unite Us org-wide cases API (dashboard-backed)

Status: **investigation for a NEW feature** (not related to the migration
detection work). This just documents what the Unite Us "Cases" dashboard calls,
so we can build on it.

## Endpoint

The dashboard `https://app.uniteus.io/dashboard/cases/{open|closed|all}` is
backed by an org-wide cases list on the core API. Verified from a live capture:

```
GET https://core.uniteus.io/v1/cases
    ?filter[provider]=12706c81-03a1-4cdb-954a-579929cd05df   # our org (== cred.provider_id)
    &filter[state]=managed,pending_authorization
    &filter[has_outcome]=false            # Open tab (true = Closed; omit = All)
    &filter[updated_after]=<ISO8601>      # incremental delta
    &filter[include_pathways]=false
    &sort=opened_date&sort_direction=desc
    &page[number]=N&page[size]=50
```

Auth is the same agent Bearer + `x-provider-id`/`x-employee-id` the rest of the
core client already uses; `cred.provider_id` supplies the provider filter.

## Filters (confirmed / to confirm)

- `filter[provider]` — org scope. Confirmed.
- `filter[state]=managed,pending_authorization` — confirmed (core 400s on other
  `state` values; richer status lives in `internal_state`).
- `filter[has_outcome]` — open/closed discriminator: `false` = Open, `true` =
  Closed. A closed case stays `state=managed` with a non-null `closed_date` + an
  `outcome`.
- `filter[updated_after]=<ISO8601>` — incremental (only cases changed since a
  timestamp).
- Narrowing filters to CONFIRM the exact param name (from a Network capture) +
  resolve the ids:
  - primary worker — all our cases are assigned to **Elorr Arama**
    (`eascn@metcouncil.org`, employee id `b04da8b0-71d0-454c-9ec3-3b18c78b3a56`).
    Likely `filter[primary_worker]` (could be `filter[worker]`/`filter[employee]`).
  - service type — restrict to our food services: **Medically Tailored Meals,
    Prepared Meals, Produce Prescription/Voucher**. Likely `filter[service]=<ids>`;
    map the three names -> service ids (via `/services` or a capture).

## List vs details (CONFIRMED from a saved response)

`/cases` is a LIST of case summaries + relationship IDs + paging meta -- NOT
expanded related details:
- per case: own `attributes` (`state`, `resolution`, `description`,
  `opened_date`, `closed_date`, `created_at`, `updated_at`) + relationship IDs
  (`person`, `program`, `service`, `service_authorization`, `primary_worker`,
  `network`, `provider`).
- `meta.page`: `number`, `size`, `total_pages`, `total_count` (e.g. 285 pages /
  14244 count) -- total known up front.
- `created_by` is NOT present anywhere in the payload (confirmed 0 occurrences).

To get related DETAILS (person name/DOB, auth amount/dates, program name):
- `include=person,service_authorization,program,service,primary_worker` ->
  returned in `included[]` in the same response; OR
- `GET /cases/<case_id>` per case.

## More filters (confirmed valid from captured URLs)

- `filter[state]=draft` is also valid (draft cases).
- `filter[service]=<id,id,id>` -- our 3 food service types:
  - Medically Tailored Meals / Prepared Meals / Produce Rx-Voucher (map exact
    name->id):
    `edb0ff4f-745c-4c1e-84aa-614f086caf88`,
    `1f2f3403-f475-425b-b637-2a8dc6b9d79f`,
    `155847fc-cddb-469b-8dca-50339cd5b6a6`
- `filter[service_authorization.state]=approved,requested,denied` -- nested
  filter on the case's service authorization state.

## Deep link to a case (built from the list, no detail fetch)

Clicking a dashboard result navigates to:

```
https://app.uniteus.io/dashboard/cases/{open|closed|all}/{case_id}/contact/{person_id}
```

Both ids are already in the list row: `{case_id}` = `data[].id`, `{person_id}` =
`data[].relationships.person.data.id`. So we can render a direct Unite Us link
per case with nothing beyond the list response. (The `{open|closed|all}` segment
mirrors the `has_outcome` tab.)

## Response shape (per case)

```json
{
  "id": "<case_id>", "type": "case",
  "attributes": {
    "state": "managed|pending_authorization",
    "resolution": "pending|...",
    "opened_date": ..., "closed_date": null, "created_at": ..., "updated_at": ...
  },
  "relationships": {
    "person":  { "data": { "id": "<person_id>" } },
    "provider": { "data": { "id": ... } },
    "network":  { "data": { "id": ... } },
    "program":  { "data": { "id": ... } },
    "service":  { "data": { "id": ... } },
    "service_authorization": { "data": { "id": ... } },
    "primary_worker": { "data": { "id": "<employee_id>" } },
    "outcome": { "data": null }
  }
}
```

- `include=primary_worker,program,service` returns those records in `included[]`
  in the same payload (employees expose `first_name`/`last_name`, not `name`).
- Pagination: `meta.page.total_pages`; `page[size]` up to ~100.

## Scale note

Observed on Unite Us: the Open filter shows **"1-50 of 14243 open cases"** — so
~14.2k open cases org-wide. The total count comes back in the response `meta`
(so we know the total up front, not just `total_pages`). That's ~285 pages at
`page[size]=50` (~143 at size 100) for ALL open; closed is a separate count.

Provider-scoped, and optionally narrowed by worker + service-type, keeps the
relevant pipeline much smaller; incremental via `updated_after` makes routine
runs cheap.

## Filtering by OUR agents == the SAME filter the CSV import applies

Decision: "our cases" means exactly what the **CSV cases import** already
enforces -- filter by the **CREATOR**, not the assigned worker.

Import rule (see `api/services/csv_import.py` ~1160-1203): keep a case only when
`case_created_by_id` (== `Case.created_by_id`) is in the set of
`UniteUsAgent.user_id` for agents whose `originating_team` is a CareCircle team
(`CARECIRCLE_ALLOWLIST_TEAMS` = CareCircle Call Center + CareCircle Street; Met
Council excluded), plus the Met Council provider gate. Empty roster = accept all.

So the dimension is **`created_by` in {our CareCircle agents' `user_id`s}** --
NOT `primary_worker`. (The `primary_worker`/`employee_id` filter the dashboard
used, e.g. Elorr Arama `b04da8b0-...`, is a DIFFERENT dimension and does not
match the import.)

Comma-list filters accept **max 50 ids** per request (confirmed for
`primary_worker`; assume the same for any creator filter) -> batch our user_ids
in groups of <= 50 and union.

KEY OPEN QUESTION: can the org `/cases` endpoint filter/return `created_by`?
Findings so far:
- The dashboard UI does NOT expose a "Created by" filter (user checked).
- The captured request only had `provider`/`state`/`has_outcome`/`updated_after`/
  `primary_worker`, and the case payload did NOT include a `created_by` field.
- BUT the dashboard UI != the full API, so TEST the raw URL directly:
  - `&filter[created_by]=<user_id>` (also try `filter[user]` / `filter[creator]`)
    -> if honored, pass our `user_id`s batched <= 50 (exactly replicates import).
  - `&include=created_by` -> if a created_by relationship appears, we can page
    provider cases and match client-side on our `user_id`s.
- If neither works:
  - Proxy by `filter[primary_worker]=<Elor employee_id>` (all our cases are
    assigned to Elor; Met Council's are not) -- CLOSE but it's the ASSIGNED
    dimension, not the import's CREATED_BY, so it can diverge.
  - Or resolve created_by per case via case detail -- accurate but defeats the
    bulk efficiency.

(`UniteUsAgent.employee_id` + `filter[primary_worker]` stays available if we ever
want the assigned-worker dimension instead.)

## Clients / people (research -- IN PROGRESS)

What we know so far:
- Extension only calls `GET /people/<id>` (single, `?include=addresses`) + scrapes
  the facesheet DOM (`/facesheet/<client_id>`). No org-wide people/contacts LIST.
- Server-side wraps only single-person fetches (`get_person`, `get_consent`,
  `list_insurances`, `list_record_languages`). No `/people` list wrapper.

Two ways to enumerate "our clients":
1. DERIVE FROM CASES (no new endpoint): the cases sweep already returns
   `relationships.person.data.id` per case -> dedupe = our client set. Add
   `include=person` to the cases call for inline person attributes, or
   `GET /people/<id>` for full detail. Cheapest; reuses the cases work.
2. DEDICATED CONTACTS/PEOPLE LIST endpoint -- CONFIRMED: there is NO provider-
   scoped people master list. The `/dashboard/clients/all` page is itself DERIVED
   FROM CASES: it takes the person ids from the (provider-scoped, filtered) cases
   list and batch-hydrates them via:

       GET https://core.uniteus.io/v1/people?filter[id]=<person_id,person_id,...>
           &page[number]=1&page[size]=N

   (observed `page[size]=34` == the number of ids fetched.) So `filter[id]` takes
   a comma-separated id list (batch <= ~50 like the other comma filters; confirm
   the exact cap). This is exactly how our derive-from-cases flow should hydrate
   clients -> no separate clients-list exploration needed.

Canonical clients flow (confirmed): cases sweep -> dedupe `person.data.id` ->
`GET /people?filter[id]=<batch>` to hydrate full client records. (Single-person
`GET /people/<id>` still exists; the batch form is `filter[id]`.)

## TODO (once the feature is defined)

- Confirm the exact `primary_worker` / `service` filter param names + ids.
- Decide what the new feature actually does with this data (TBD — see the
  feature spec, not this file).

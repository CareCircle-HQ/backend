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

Provider-scoped (optionally worker + service-type filtered) means the whole
managed pipeline is a few hundred paged calls, or incremental via
`updated_after` — cheap enough for a routine job.

## TODO (once the feature is defined)

- Confirm the exact `primary_worker` / `service` filter param names + ids.
- Decide what the new feature actually does with this data (TBD — see the
  feature spec, not this file).

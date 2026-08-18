# Design (brainstorm): live API sync to replace the daily export/import

Status: **brainstorm / not started** — captured to resume later. Builds on
`docs/uniteus_org_cases_api_investigation.md`.

## Goal

Stop the manual daily CSV export/import. Keep our DB current with Unite Us by
pulling changes directly from the core API.

- The extension already captures the current client/case the agent is viewing —
  that part is fine, nothing to change.
- The gap: a client already in our DB whose data changes in Unite Us (case
  opened/closed, auth changed, etc.) needs to flow in without a CSV round-trip.

## Core shift: export/import -> incremental API sync

The CSV export does two jobs: (a) DISCOVER new/changed cases org-wide, and
(b) carry the field data. Both are now API-reachable:

- **Discovery + change detection = org cases sweep with `filter[updated_after]`.**
  Keep a `last_synced_at` watermark; each run pulls only cases changed since:
  `GET /cases?filter[provider]=…&filter[service]=<3 food ids>&filter[updated_after]=<watermark>&page…`
  Cheap (a few pages, not 285). This is the discovery today's `run_daily_pull`
  lacks (it only iterates STORED client ids).
- **Hydrate people:** dedupe `person.data.id` -> `GET /people?filter[id]=<batch <=~50>`.
- **Reconcile** each affected client via the EXISTING pipeline
  (`_process_person`/`_process_case`/`refresh_from_uniteus` -> map -> serializer
  -> case_events -> `reconcile_enrollment_authorization`), plus per-client
  insurance/coverage/notes (already API in the daily pull).

Bonus: migration detection falls out for free (`person.data.id` vs stored client).

## CENTRAL INVARIANT (resolves the case-completeness risk)

> Delta = "which clients to look at." Per-client FULL case fetch = "what's
> actually true." Reconcile ONLY from the full set — never from the partial
> delta rows.

The moment a delta flags a client, re-fetch that client's COMPLETE case set and
reconcile from the whole picture:
```
GET /cases?filter[person]=<person_id>     # ALL cases, no state filter (union)
```
(the extension's `apiFetchCaseList` already unions every state). Then:

- Only close an enrollment when the client's FULL case set has NO open governing
  case — never because one case-close row appeared in a delta. Prevents the
  premature "stop service" problem.
- Late-arriving opens are handled: if a new open case shows up in a later run,
  `replace_enrollment_for_case_change` already carries service/verification/
  dietary/kitchen from the closed enrollment to the new one. Re-runs converge
  (idempotent).

## Gotchas

- **Do NOT filter the delta to open-only.** With `has_outcome=false`/open-only, a
  case that just CLOSED drops out of the delta -> we'd miss the close. For SYNC,
  page by `updated_after` across ALL states (provider + service scope only). The
  open/closed narrowing is for the dashboard, not sync.
- **Full sweep still needed periodically** (weekly?): incremental misses
  disappearances / cases that fall out of scope after closing; a full sweep
  guarantees every client's complete set is eventually reconciled.

## Decisions to nail (open)

1. **Scope filter for "our cases."** `created_by` (the CSV import's key) is NOT
   server-filterable and NOT in the payload. Candidate scope: `provider +
   service(3 food ids) + (all states)`. CONFIRM: do all our cases fall under
   exactly those 3 service ids (MTM / Prepared Meals / Produce Rx-Voucher)? Add
   `primary_worker=Elor` if needed. (Service ids in the investigation doc.)
2. **Frequency + full-sweep cadence.** Incremental is cheap enough for hourly;
   add a weekly full sweep for disappearances. Confirm cadence.
3. **Token / auth (biggest risk).** The API needs a LIVE agent token (captured by
   the ext while an agent browses). An unattended hourly/nightly job needs a
   fresh, valid token; refresh tokens are single-use + shared with the browser
   session. How do we guarantee a usable token for the scheduled job (dedicated
   service login? only run while a session is fresh? refresh strategy)?
4. **Which datasets move to API vs stay CSV.** Cases/people/insurance/coverage/
   notes/consent are API-reachable. Screenings live on a DIFFERENT host
   (`screenings-ingestion`); assessments/eligibility are their own flows. Move
   everything, or keep CSV for screenings/assessments initially?

## Suggested first slice

Cases sync scoped by `provider + service`, paged by `updated_after` (hourly),
reusing the existing per-case reconcile + `/people?filter[id]` hydrate, honoring
the CENTRAL INVARIANT (full per-client case fetch before any close/replace), plus
a weekly full sweep. That alone kills the daily case export/import. Layer
screenings/assessments after. Lock the scope filter + token strategy first.

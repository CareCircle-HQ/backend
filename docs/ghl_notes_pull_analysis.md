# GHL Contact Notes — Pull Feasibility Analysis & Resume Plan

> Status: **analysis complete, build not started.** Paused to fix a couple of
> prerequisites (see "Blockers to fix"). This doc captures everything verified
> live on 2026-07-14 so we can resume without re-investigating.

## Goal
Pull **notes for members/contacts** from the GoHighLevel (GHL / LeadConnector)
CRM into our platform (e.g. to surface on the member profile).

## Verdict
**Feasible.** The Contact Notes endpoint works, the existing private token has
the scope, and notes contain real data. Two prerequisites must be fixed before
a member-facing pull (see Blockers). Member→contact mapping is intentionally
out of scope for the first pass — a raw "pull all notes" scan needs no mapping.

---

## What was verified live
- **Auth/config already work for inbound reads.** Private Integration Token
  (`GHL_PRIVATE_TOKEN`) + `GHL_LOCATION_ID`, `Bearer` + `Version: 2021-07-28`,
  base `https://services.leadconnectorhq.com`. Headers built in
  `api/integrations/ghl/config.py::headers()`.
- **`CRM_SYNC_DISCONNECTED` (default True) gates only OUTBOUND writes.** Reads
  are unaffected — confirmed (this is also documented in `pull_ghl_contact.py`).
- **Notes endpoint returns 200** (scope confirmed):
  - List: `GET /contacts/{contactId}/notes` → `{ "notes": [ ... ] }`
  - Single: `GET /contacts/{contactId}/notes/{noteId}`
- **Notes exist and carry useful content**, but are **sparse**: the default
  contact search order front-loads old, note-less lead contacts. First
  note-bearing contact appeared after scanning **635** contacts.
- **Location scale:** `36,325` contacts in the location vs `32,108` local
  `Client`s → our members were very likely pushed to GHL at some point.

### Note payload shape (real example)
Contact `TJ5dZdafNpomNydJRgto`:
```json
{
  "id": "My80dyKzvpBGfd9nAmMR",
  "body": "<p ...>8458265124</p>",       // HTML
  "bodyText": "8458265124",               // plain text
  "title": "husband number",
  "userId": "IDkzg1o3pquYiAmvr5Z8",       // author = GHL USER id (not our Agent)
  "dateAdded": "2026-07-09T13:58:04.109Z",// UTC ISO
  "contactId": "TJ5dZdafNpomNydJRgto",
  "color": "#fef7c3",
  "pinned": false,
  "relations": [{ "objectKey": "contact", "recordId": "TJ5dZdafNpomNydJRgto" }]
}
```

### Two different "notes" concepts in GHL (don't confuse them)
| Kind | Where it lives | Status |
|---|---|---|
| **Timeline Contact Notes** | dedicated `.../notes` endpoint (above) | freeform, sparse, this doc's focus |
| **Structured "Notes - …" custom fields** (`Notes - Eligibility`, `Verification Notes`, etc.) | inside the contact payload's `customFields` | **already retrievable** via `pull_ghl_contact` (contact GET) |

---

## Phase 0 results (updated 2026-07-15)
Built `api/management/commands/scan_ghl_notes.py` — a read-only, throttled,
location-wide scan that pages all contacts (`searchAfter` deep pagination),
pulls `/contacts/{id}/notes`, and — for each **note-bearing** contact — also
`GET`s the contact to extract mapping fields and resolve our local `Client`.
Dumps JSONL (`tmp/ghl_notes_dump.jsonl`) + a `.summary.json`.

### Confirmed live
- **Notes carry no member id** — only `contactId` + `userId`. Mapping requires a
  second `GET /contacts/{contactId}`.
- **There is NO "Unite Us member id" field.** The contact instead stores **our
  own Enrollment Platform Client ID(s)** = local `Client` UUIDs. So a note maps
  straight to our DB, no Unite Us id needed.
- **A GHL contact = a whole household.** It has `enrollment_client_id` (primary)
  + `HM #2..#10 - Enrollment Platform Client ID` (10 member-id fields total) +
  per-service Case IDs (`Enrollment Platform Case ID - Internal Services / …`).
  **Notes are therefore household-level, not member-level.**
- **`body` is HTML** (`<p style="…">value</p>`); **`bodyText` is the clean
  plaintext** — prefer `bodyText`; only sanitize `body` if rich text is wanted.

### Mapping reliability (the "clean up" problem)
Resolve a note's contact to a local `Client` with a fallback ladder (implemented
in `scan_ghl_notes._match_local_client`): `enrollment_client_id` → `HM #N` id →
`email` → `phone`. Observed caveats on real data:
- **Only the primary `enrollment_client_id` is usually populated**; the `HM #N`
  fields were empty on every household checked → non-primary members not
  resolvable from custom fields (need phone/email).
- **The stored id can be stale** — 3 of 4 email-matched members had an
  `enrollment_client_id` pointing to a *different* local `Client` UUID (older/
  duplicate rows, or the email belongs to a non-primary member).
- **Note-bearing leads have nothing populated** (phone-only contacts); they
  still map via the **phone** fallback (verified: a lead resolved to a local
  `Client` by phone).
- Watch for **our own duplicate `Client` rows** (same person across enrollments)
  when choosing which record to attach a note to.

### Throughput reality
Effective rate is **~1 contact/sec** (network round-trip latency dominates, not
the 8–9 req/s throttle). A full 36,361-contact scan is therefore **several
hours**, not ~75 min. Run it detached (nohup/screen) or in batches. It flushes
per note-bearing contact, so partial runs keep their dump.

---

## Blockers to fix (the "couple of things")
1. **Member→contact mapping is not wired.**
   - **No local `Client` has `crm_contact_id`** (0 of 32,108) because outbound
     sync was hard-disconnected during the MVP, so ids were never written back.
2. **The enrollment-client-id search filter is broken.**
   - `pull_ghl_contact._search_by_enrollment_id` filters by the raw custom-field
     id and GHL returns **`400 "Invalid field xac7ac5fVHKyutg0mrB6"`**.
   - The command swallows non-200s and returns `None`, so the lookup silently
     fails (0/30 clients matched in testing).
   - Fix: use the correct v2 custom-field search syntax (e.g. `customFields`
     filter / `fieldKey`), or map by **email/phone** instead.
3. **Author name resolution (optional).** `userId` is a GHL user, not our
   `Agent`. To show a name, add `GET /users/{userId}` (scope `users.readonly`)
   or accept raw ids.

---

## Recommended approach (when resuming)
**Phase 0 — raw scan (no mapping needed).**
Page every contact (`POST /contacts/search`, `pageLimit: 100`), call
`GET /contacts/{id}/notes`, and dump every note (`id`, `contactId`, `title`,
`bodyText`, `dateAdded`, `userId`, `pinned`) to `tmp/ghl_notes_dump.json`.
Purpose: measure real volume + content across all 36,325 contacts.
Throttle to ~8 req/s (LeadConnector burst limit ≈ 100 req / 10s).

**Phase 1 — mapping.** Fix blocker #2 (or map by email/phone) so a member
resolves to its contact id; optionally backfill `Client.crm_contact_id`.

**Phase 2 — surface.** Decide live passthrough vs. mirrored store (like
`crm_contact_id`) and render notes on the member profile.

---

## Reference — IDs, endpoints, files
**Constants**
- Enrollment Platform Client ID custom field on GHL contact:
  `xac7ac5fVHKyutg0mrB6` (a.k.a. `contact.enrollment_client_id`; value = local
  `Client` UUID).
- API base: `https://services.leadconnectorhq.com` · Version `2021-07-28`.

**Endpoints used/needed**
- `POST /contacts/search` — body `{ locationId, page, pageLimit, query?, filters? }`
- `GET /contacts/{id}` — full contact incl. `customFields`
- `GET /contacts/{id}/notes` — **timeline notes** (target)
- `GET /locations/{locationId}/customFields?model=contact` — field catalog
- `GET /users/{id}` — resolve note author (optional)

**Code**
- `api/integrations/ghl/config.py` — token/location, `headers()`, `is_enabled()`
- `api/integrations/ghl/contacts.py` — outbound `sync_client` (contact upsert)
- `api/management/commands/pull_ghl_contact.py` — read-only contact pull +
  contact-resolution fallbacks (has the broken enrollment-id search)
- `docs/ghl_field_mapping_analysis.md` — existing field-mapping notes

**Env / settings** (`backend/settings.py`, from env)
- `GHL_PRIVATE_TOKEN`, `GHL_LOCATION_ID`, `GHL_API_BASE`, `GHL_API_VERSION`
- `CRM_SYNC_DISCONNECTED` (default `True`, outbound only), `CRM_SYNC_ENABLED`

**Run reads locally**
```bash
.venv/bin/python manage.py shell -c "..."   # note: bare `python` is not on PATH
```

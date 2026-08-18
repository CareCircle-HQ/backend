# Unite Us person-migration: detection & auto-merge plan

Status: **paused / in progress**. This captures decisions + remaining work so we
can resume later.

## Problem

When Unite Us migrates a person to a NEW canonical id, `GET /people/<old>`
returns a **301** to the new person and the person's CASES re-parent to the new
id. Our pull then creates a NEW client (holding the imported cases) while the OLD
client keeps our internal service state (enrollment/household/delivery). That
"tears" the link: **`enrollment.client != enrollment.case.client`**.

`merge_migrated_client` heals this (moves service/history/agent state onto the
NEW survivor, stamps `new.migrated_from_id = old`, tags `Migrated`, deletes the
emptied old client + household). Today it's only triggered by the browser
extension (live 301) or a manual CSV (`merge_migrated_clients`). Anything
migrated **before** detection existed is unmerged and shows the torn link.

Scale: ~12k active members, some already migrated.

## Key insight

The extension is **not required**. Unite Us signals the migration itself via the
301, and `requests` follows it, so `get_person(old)` already returns the NEW
person (its `data.id` is the new id). We can detect a migration by comparing the
**requested id vs the returned `data.id`** — and our own DB already reveals it via
the torn link (`enrollment.case.client` = the new id).

## Decisions

- **Auto-merge identity gate (strict):** merge only when OLD and NEW match on
  **DOB + first name + last name + Medicaid member id** (`Insurance.external_member_id`
  of a MEDICAID plan). Case/whitespace-insensitive on names; a missing field
  fails the gate. This keeps same-DOB twins/siblings and blank-Medicaid records
  out of auto-merge. Implemented as `client_migration.identity_matches_for_merge`.
- **Rollout: flag-only first.** Detect + tag `Need Review`; do NOT merge until a
  settings flag is flipped on after watching a few nights.
- **Do NOT blanket API-probe all 12k nightly** (hours of calls, rate limits,
  touches the shared single-use refresh token). Fold detection into the pull
  (which already visits each member) instead.

## Already done (committed to `origin/dev`)

- `list_unmerged_migrations` (`2e8ce6c`/`3afeebb`): read-only DB torn-link
  detector; splits MIGRATION (same DOB) vs REVIEW (dob differs). `--ids-only`.
- `detect_uniteus_migrations` (`2e8ce6c`): read-only API probe over members with
  an open internal-service case; prints `old -> new` + gate result
  (MATCH/REVIEW/no-local/self). Flags: `--limit`, `--provider-id`, `--client-id`.
- `list_active_enrollment_no_internal_case` (`d899362`): the "case moved away"
  anomaly (prime migration suspects).
- Service helpers in `api/services/client_migration.py`:
  `detect_api_migration(api, client)` and `identity_matches_for_merge(old, new)`
  (+ `_medicaid_member_id`). Tests in `DetectApiMigrationTest`.

## Remaining work

### 1. Ongoing detection inside the nightly pull (highest leverage, ~zero cost)

Hook: `DailyPull._process_person(client_id)` in `api/services/uniteus_import.py`,
right after:

```python
person = self.api.get_person(client_id)   # already follows the 301
data   = person.get("data") or {}
```

Add:

```python
returned_id = str(data.get("id") or "")
if returned_id and returned_id != str(client_id):
    self._record_migration(old_id=str(client_id), new_id=returned_id)
```

`_record_migration` (flag-only first):
- log it + add to the `ImportRun` summary (nightly report lists old -> new);
- tag the member `Need Review` (surfaces in Care Management);
- do NOT set `migrated_from_id`, do NOT merge;
- idempotent no-op if already merged (`migrated_from_id` set / old gone).

Later, behind a settings flag (default OFF), the same hook does the merge:

```python
if settings.MIGRATION_AUTO_MERGE and identity_matches_for_merge(old, new):
    merge_migrated_client(old, new, actor_label="pull:uniteus-migration")
```

Notes / edge cases:
- Today the pull already upserts the NEW client because `map_person_to_client`
  keys off `data["id"]` while `existed` is looked up under the OLD id
  (`uniteus_import.py` ~396-413) — that mismatch is what creates the duplicate.
- The OLD client drops out of the pull loop once its cases move, so the 301
  capture reliably fires on the FIRST pull after the migration; older ones are
  covered by the DB detector.

### 2. Backlog (already migrated)

- Run `list_unmerged_migrations` (free, instant) to clear everything already
  reflected in our data.
- Optionally one-time scoped API backfill via `detect_uniteus_migrations`
  (`--limit`/`--provider-id`) for stragglers not yet reflected; or just let the
  pull hook catch them.

### 3. Auto-merge enablement

- Add settings flag `MIGRATION_AUTO_MERGE` (default False).
- Flip on after flag-only has run a few nights and the `Need Review` list looks
  correct.

## Relevant files

- `api/services/client_migration.py` — merge + detection + gate helpers.
- `api/services/uniteus_import.py` — `DailyPull._process_person` (hook), `run_daily_pull`.
- `api/integrations/uniteus/api.py` — `UniteUsClient.get_person` / `core_get` (follows 301).
- `api/views_uniteus.py` — existing ext-triggered merge (old_id/new_id).
- `api/management/commands/` — `list_unmerged_migrations`, `detect_uniteus_migrations`,
  `merge_migrated_clients`, `list_active_enrollment_no_internal_case`.

## Next step when resuming

Implement **step 1 flag-only** (`_record_migration` + the compare in
`_process_person`), with the `MIGRATION_AUTO_MERGE` flag scaffolded OFF and the
gate wired but not firing. Add tests. Run `manage.py check` + full suite.

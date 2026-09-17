# Working notes for agents

## Shell conventions (user preference)

- **Do not prefix commands with `cd ~/backend && .venv/bin/python`.** Assume the
  shell is already in the backend directory with the virtualenv active, and
  write `python manage.py ...`.
- **Give single-line commands.** Multi-line commands with trailing `\` get
  flattened on paste, so the backslash escapes a space instead of a newline and
  the command breaks. For the same reason, never put a trailing `# comment` on a
  command line -- it ends up as arguments.
- Be explicit about **where** a command runs: the EC2 box (`ubuntu@…:~/backend`)
  or the local Mac (`/Users/alex/Projects/ext/backend`). They have separate
  databases; the local one is a periodically refreshed CLONE of production.

## Frontend: `apiFetch` stringifies the body FOR you

`api.ts` does `body: JSON.stringify(body)` internally, so callers pass a plain
object:

```ts
apiFetch("/settings/vendors/", { method: "POST", body: { name } })    // correct
apiFetch("/settings/vendors/", { method: "POST", body: JSON.stringify({ name }) })
```

The second double-encodes: the server receives a JSON *string* rather than fields,
DRF finds no `name`, and it fails with a validation error that looks like a
backend bug. It broke vendor creation on 2026-09-17 and was present in five call
sites, because the mistake is invisible at the call site and the API tests -- which
post proper objects -- all passed.

The raw `fetch` in `startReportExport` DOES stringify its own body; that one is
correct.

## Verification

```
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test api.tests --parallel 4
```

### Four tests fail after 20:00 EDT — a UTC/local date boundary, not your change

Between 20:00 EDT and midnight, `timezone.now()` is already TOMORROW in UTC while
`timezone.localdate()` is still today (`TIME_ZONE = America/New_York`). Tests that
build a date as `timezone.now() - timedelta(days=1)` and expect it to read as
"yesterday" therefore get TODAY, and any "has it expired?" assertion inverts:

```
local: 2026-09-16 20:29 EDT      timezone.now()   -> 2026-09-17 00:29 UTC
                                 localdate()      -> 2026-09-16
```

Known to affect, verified failing on a CLEAN tree at 00:29 UTC:

```
ResumeRevalidationTest.test_expired_authorization_blocks
ClientAddedAtAndSourceTest.test_data_page_member_created_filter_uses_the_added_date
CalendarKeepsOccurrencesOnExclusionTest.test_sync_active_calendars_heals_fully_lapsed_calendar
CaseInsuranceCoverageTimelineMetadataTest.test_coverage_event_records_status_and_expiry
```

Before assuming you broke them, `git stash -u` and re-run: that is how these were
identified. The real fix is for those tests to build dates from
`timezone.localdate()` rather than `timezone.now()`, which is worth doing but is
not urgent.

**Migrations are DISABLED under `manage.py test`** (`_DisableMigrations` in
settings builds tables straight from model state). So data migrations never run
in the suite: a test can never assert on rows a migration seeds, and must create
them itself. Verify a data migration by running it against the local clone.

### Frontend (fixed 2026-09-16 -- this section used to say the opposite)

```
npx vitest run       # passes
npm run typecheck    # tsc --noEmit
npm run build        # esbuild
```

`npm test` used to FAIL on its own -- `ClientStageProgress.test.tsx` rendered a
component calling `useNavigate()` outside a `<Router>`, so all 12 tests failed and
the only frontend test file caught nothing. Fixed by passing a `MemoryRouter`
through Testing Library's **`wrapper` option** rather than wrapping the element:
`wrapper` is reapplied by `rerender()`, and three tests call it, so inline
wrapping would have left those failing.

TypeScript **is** now installed (it was not, so `tsc --noEmit` silently did
nothing). It is pinned in `devDependencies` with a deliberately LOOSE
`tsconfig.json` -- `noEmit`, `strict: false` -- because the codebase was written
without a checker and `strict` would produce hundreds of errors and be switched
off again. It catches the class of mistake that was previously invisible: a
reference to a type that does not exist, a misspelled property, a bad import
path.

**`npm run build` alone does NOT type-check.** esbuild strips types without
checking them, so a reference to a non-existent type builds cleanly. That shipped
a real bug on 2026-09-16 (`CaseRow` where the type is `MemberCase`). Run
`typecheck` too.

There are **14 pre-existing errors** -- including `EnrollmentsTab.tsx`
referencing a `Validation` type that does not exist. They are known and left
alone; `typecheck` is not clean, so compare the COUNT before and after your
change rather than expecting zero.

Use **pnpm**, not npm, for installs: `node_modules` is a pnpm store and
`npm install` fails with "Cannot read properties of null (reading 'matches')".

## Celery beat: the scheduler, and why nothing was scheduled

Until 2026-09-15 there was NO beat. `celery-worker` runs without `-B` and no beat
unit existed, so every entry in `CELERY_BEAT_SCHEDULE` had never executed. Nine
tasks were silently inert, which explains a day of symptoms that each looked like
a separate bug:

```
rebuild-enrollment-analytics  hourly :20  -> read model found 26-28h stale
warm-dashboard-cache          every 8m    -> /portal/dashboard/ at 8-10s
poll-uniteus-exports          every 5m    -> Settings > Import never advanced
publish-health-metrics        every 15m   -> metrics only when run by hand
sync-delivery-calendars       daily 05:00 -> 19 stranded households
sweep-closed-case-service     daily 04:00 -> a 1,476-member backlog
process-reauthorization-extensions        -> 237 parked, none processed
import-uniteus-assessment-results         -> flag off, no-op anyway
sync-member-warnings          daily 11:00 -> the ONLY one covered, via cron
```

The single actual cron entry is `sync_member_warnings.sh` at 03:00;
`daily_pull.sh` is COMMENTED OUT, so the Unite Us daily pull only happens when an
agent clicks refresh.

`deploy/celery-beat.service` is the unit to install. Separate from the worker, NOT
`-B`: embedded beat dies with `--max-tasks-per-child` recycling and every deploy
restart, and two workers would each run their own scheduler and fire everything
twice.

**Before enabling beat, dry-run the state-changing tasks.** Eight of the nine are
safe or no-ops, but `sweep-closed-case-service` had accumulated 1,476 candidates
-- 20 of them with 8-45 deliveries already scheduled -- and would cancel the lot
in one overnight pass. It is gated behind `CLOSED_CASE_SWEEP_ENABLED` (default
off) for exactly that reason. Most of these commands dry-run by omitting
`--apply`:

```
python manage.py stop_closed_case_service --all          # no --apply = dry run
python manage.py process_reauthorization_extensions      # no --apply = dry run
```

## The read model is served from a REPLICA -- pin verification to the primary

`EnrollmentAnalytics` READS are routed to the `replica` database whenever
`REPLICA_DB_HOST` is set (`api/db_routers.AnalyticsRouter`, "keeping heavy
analytics reads off the primary"). Writes -- including `rebuild()` -- go to the
primary, which pins itself to `default` so it never computes from a lagging copy.

**So you cannot verify a write by reading the default connection.** A check like

```
EnrollmentAnalytics.objects.filter(service_type='').count()      # reads the REPLICA
```

answers "what did the replica have a moment ago", not "what did the rebuild do".
On 2026-09-15 that cost real time: the same count read 3, then 10, then 21 within
a few minutes, and a repair loop never converged because the list of rows to fix
was ALSO being read from the replica -- so each pass rebuilt a stale, partial set.

Always pin both the diagnosis and the verification:

```
E.objects.using('default').exclude(company_status='no_case').filter(service_type='')
```

Consequence worth knowing: `ReadModelAgeHours` in `health_metrics` reads the
replica, so it measures REBUILD AGE PLUS REPLICATION LAG. That is arguably the
more honest number -- it is what the Data page actually shows a user -- but do not
read it as "how long ago did the rebuild finish".

## Long-running commands on production

**Run full-table jobs OFF-HOURS.** A `rebuild_enrollment_analytics --prune`
(76k rows, ~12k rows/5min) measurably degrades agents while it runs -- observed
2026-09-15 12:34-12:37:

```
POST .../refresh-uniteus/   6915-12657ms   (normally 3000-4000ms)
GET  /api/portal/dashboard/ 7957-9892ms    x6 from ONE agent retrying
```

It tripped `app-slow-request-rate`. It does NOT cause 5xx -- the 502s that day
were a `systemctl restart gunicorn` deleting the unix socket, a separate fault
(fixed: `deploy.sh` now reloads).

**Detach properly, and unbuffer.**

```
setsid nohup python -u manage.py <cmd> > /tmp/<cmd>.log 2>&1 < /dev/null &   # EC2 only
nohup python -u manage.py <cmd> > /tmp/<cmd>.log 2>&1 < /dev/null &          # local Mac
```

- `-u` -- without it Python BLOCK-BUFFERS stdout into the file, so if the process
  dies the log is EMPTY and you learn nothing. Happened twice on 2026-09-15.
- `setsid` -- `nohup` alone blocks SIGHUP but systemd-logind can still reap a
  session's processes; a rebuild vanished mid-run this way, silently.
- **`setsid` does NOT exist on macOS.** It fails with
  `bash: setsid: command not found` -- and because that happens BEFORE the
  redirect, the log file is never created and the command silently never starts.
  Cost ~5 minutes on 2026-09-16 waiting on an import that was never running.
  Always confirm a background job actually started (`pgrep -f`), rather than
  assuming the `&` succeeded.

**Check for a duplicate before starting one.** Two concurrent `--prune` passes
delete each other's rows:

```
pgrep -af rebuild_enrollment_analytics
```

**Do not deploy while a long import runs.** `deploy.sh` restarts `celery-worker`;
SIGTERM gives Celery a warm shutdown but systemd SIGKILLs at `TimeoutStopSec`
(90s), so a long task dies mid-run -- that is what produced "Worker restarted
mid-run" on ImportRun #1449 (member_prep, 8000/15148 rows).

## A rebuild applies the code that is RUNNING -- deploy first

A `rebuild_enrollment_analytics` (or any repair loop) executes the deployed code,
not the code on your branch. Running it before the deploy produces a confident,
wrong "verified" result. This bit three times in two days -- the `medicaid_id`
backfill, the Meals/Boxes service-type fix, and the ticket-type filter, where a
production rebuild produced 782 rows instead of ~11,000 because the fix was still
sitting on `dev`.

The tell is a metric that moves the wrong DISTANCE rather than not at all. Look
for a second, independent signal before concluding the data is fixed: for the
ticket-type work it was 25 rows with duplicate codes, which only the undeployed
`order_by()` fix could have cleared.

```
git -C ~/backend log --oneline -1     # what is actually running
```

## Case timestamps: most are SOURCE data, not ours

`Case.case_created_at` and `Case.updated_at` are **Unite Us fields** --
`auto_now`/`auto_now_add` are both False and both are nullable. So
`case_created_at` is when UNITE US created the case, which can be months before
we ever saw it, and `updated_at` is routinely NULL.

**`added_to_system_at` is the ingestion timestamp** -- "when did WE learn about
this". On 2026-09-16 reading `case_created_at` as local led to "these housing
cases arrived in August through a path with no scope check", when in fact they
were stored that same afternoon (`added_to_system_at = 09-16 17:48`) and had
simply existed in Unite Us since August. The scope guard had been working
correctly all along.

## The cases CSV import has FIVE gates, in order

A skipped row does NOT mean the classification rejected it. `_import_cases`
drops rows in this sequence, all counted identically as "skipped":

```
1  case_id present
2  Met Council is the MANAGING provider   provider_id / provider_name;
                                          originating_* is deliberately IGNORED
3  creator is on a CareCircle team        UniteUsAgent.originating_team in
                                          CARECIRCLE_ALLOWLIST_TEAMS (195 ids on
                                          the clone -- an EMPTY list means no gate)
4  case_status != "referred"
5  case_in_import_scope(service_subtype, program_name)
```

A hand-made test CSV needs `provider_name = "Met Council - SCN - PHS"` AND a
`case_created_by_id` from the allowlist, or it never reaches gate 5. Verifying a
scope change without those produces a PASS for entirely the wrong reason -- which
is what happened twice on 2026-09-16 before the third attempt actually exercised
the gate.

Note `case_in_import_scope` is applied **only** in the CSV importer. Every other
path (extension `POST /api/cases/`, Unite Us pull, admin) relies on
`CaseSerializer` rejecting `CaseType.EXTERNAL_SERVICE` -- the universal backstop.
That guard derives `case_type` ONLY when the payload omits it, so a caller that
sends its own `case_type` bypasses the classification.

`ImportRun` field names are `processed_count` / `created_count` / `updated_count`
/ `skipped_count` / `error_count` / `progress_total` / `export_type` -- not
`processed`, `errors` or `import_type`.

## Deployment

- Production runs nginx + gunicorn (unix socket
  `/home/ubuntu/backend/gunicorn.sock`) on one EC2 box, behind an **ALB that
  terminates TLS** — the vhosts `listen 80` and read `X-Forwarded-Proto`. There
  is no certbot on the box.
- `deploy.sh` deploys from **`main`**, but day-to-day commits go to `dev`, so a
  merge is needed before a prod deploy picks anything up.
- The wildcard `*.carecircleinternal.com` certificate on the ALB covers new
  subdomains, so a new hostname needs a Route 53 alias to the same ALB and an
  nginx vhost — no certificate work.

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

## Verification

```
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test api.tests --parallel 4
```

**Migrations are DISABLED under `manage.py test`** (`_DisableMigrations` in
settings builds tables straight from model state). So data migrations never run
in the suite: a test can never assert on rows a migration seeds, and must create
them itself. Verify a data migration by running it against the local clone.

`npm test` in the frontend currently FAILS on its own: the only test file
(`ClientStageProgress.test.tsx`, 12 tests) renders a component that calls
`useNavigate()` without a `<Router>` wrapper. Verified against a clean tree, so
it is not your change -- but it also means the suite catches nothing.

The frontend has **no TypeScript installed** (`node_modules/.bin/tsc` does not
exist), so `tsc --noEmit` silently does nothing. Verify it with `npm run build`
instead -- esbuild will surface syntax/JSX errors.

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
setsid nohup python -u manage.py <cmd> > /tmp/<cmd>.log 2>&1 < /dev/null &
```

- `-u` -- without it Python BLOCK-BUFFERS stdout into the file, so if the process
  dies the log is EMPTY and you learn nothing. Happened twice on 2026-09-15.
- `setsid` -- `nohup` alone blocks SIGHUP but systemd-logind can still reap a
  session's processes; a rebuild vanished mid-run this way, silently.

**Check for a duplicate before starting one.** Two concurrent `--prune` passes
delete each other's rows:

```
pgrep -af rebuild_enrollment_analytics
```

**Do not deploy while a long import runs.** `deploy.sh` restarts `celery-worker`;
SIGTERM gives Celery a warm shutdown but systemd SIGKILLs at `TimeoutStopSec`
(90s), so a long task dies mid-run -- that is what produced "Worker restarted
mid-run" on ImportRun #1449 (member_prep, 8000/15148 rows).

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

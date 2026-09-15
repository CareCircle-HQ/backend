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

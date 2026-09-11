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

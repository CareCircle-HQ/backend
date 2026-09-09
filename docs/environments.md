# Environment Configuration

CareCircle supports three environments: **local**, **dev**, and **production**.

## Quick Switch

Use the helper script to switch environments:

```bash
# Local development (default)
source scripts/switch_env.sh local

# Dev environment (AWS RDS)
source scripts/switch_env.sh dev

# Production environment (AWS RDS) ⚠️
source scripts/switch_env.sh prod
```

## Environment Details

### Local
- **Database**: Local PostgreSQL on port 5433 (or SQLite fallback)
- **File**: `.env`
- **Debug**: Enabled
- **Usage**: Day-to-day development

```bash
# Start local PostgreSQL (if not running)
brew services start postgresql@18

# Run server with local DB
source scripts/switch_env.sh local
python manage.py runserver
```

### Dev
- **Database**: AWS RDS (PostgreSQL)
- **Host**: `dev.czu4cm6ye26e.us-east-2.rds.amazonaws.com`
- **Database**: `carecircle`
- **File**: `.env.dev`
- **Debug**: Enabled
- **Usage**: Staging, testing, shared development

```bash
# Switch to dev environment
source scripts/switch_env.sh dev

# Run migrations (first time only)
python manage.py migrate

# Run server
python manage.py runserver
```

### Production ⚠️
- **Public URL**: `https://www.carecircleinternal.com`
- **Database**: AWS RDS (PostgreSQL)
- **Host**: `prod.czu4cm6ye26e.us-east-2.rds.amazonaws.com`
- **Database**: `Prod`
- **File**: `.env.prod`
- **Debug**: Disabled
- **Usage**: Live application

Required `.env.prod` web/domain settings:

```bash
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=www.carecircleinternal.com,carecircleinternal.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://www.carecircleinternal.com
# Web frontends only; chrome-extension:// origins are auto-allowed by regex.
CORS_ALLOWED_ORIGINS=https://www.carecircleinternal.com
```

The Chrome extension must also point at production: set `backendUrl` to
`https://www.carecircleinternal.com` in `extension/config.js`, and ensure
`https://www.carecircleinternal.com/*` is listed in `extension/manifest.json`
`host_permissions`.

### Delivery-partner API host (proof-of-delivery ingestion)

Delivery companies push proof of delivery on a SEPARATE hostname that exposes
nothing else — no CRM or extension route exists there. Off until configured:

```bash
PARTNER_API_HOST=partners.carecircleinternal.com
# ...and add that host to DJANGO_ALLOWED_HOSTS.
```

Do NOT add it to `CORS_ALLOWED_ORIGINS` or `DJANGO_CSRF_TRUSTED_ORIGINS`: it is
server-to-server only and must not be callable from a browser.

One-time server setup (outside the normal `git pull` deploy, so repeat it if the
box is rebuilt):

1. DNS for `partners.carecircleinternal.com` pointing at the same place as `www`.
2. Add the hostname to whatever terminates TLS in front of nginx (the vhosts
   `listen 80` and read `X-Forwarded-Proto`, so the certificate is NOT managed by
   certbot on the box).
3. Install [`deploy/nginx-partners-api.conf`](../deploy/nginx-partners-api.conf),
   which proxies **only** `/v1/` and returns a JSON 404 for everything else.

Verify after setup:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://partners.carecircleinternal.com/api/clients/  # want 404
curl -X POST https://partners.carecircleinternal.com/v1/token/ \
  -H 'Content-Type: application/json' -d '{"client_id":"...","client_secret":"..."}'
```

Credentials are issued from **Settings → Delivery Company → POD API access**
(Management only).

## Verifying the live environment

Hit the health endpoint to confirm which backend is actually responding:

```bash
curl https://www.carecircleinternal.com/api/health/
```

A live production backend returns `environment: "production"` and
`debug: false`:

```json
{
  "status": "ok",
  "environment": "production",
  "debug": false,
  "host": "www.carecircleinternal.com",
  "database": "ok",
  "server_time": "2026-06-17T20:42:00+00:00"
}
```

If `environment` is `local`/`dev` or `debug` is `true`, you are NOT hitting the
live prod environment.

```bash
# Switch to production (requires confirmation)
source scripts/switch_env.sh prod

# Run server (use gunicorn in production)
gunicorn backend.wsgi:application -b 0.0.0.0:8000
```

## Environment Files

| File | Purpose | Git Ignored |
|------|---------|-------------|
| `.env.example` | Template with all variables | No |
| `.env` | Local environment | Yes |
| `.env.dev` | Dev environment config | Yes |
| `.env.prod` | Production environment config | Yes |

## Database Configuration

All environments use environment variables:

```bash
DB_ENGINE=django.db.backends.postgresql
DB_NAME=carecircle_local
DB_USER=carecircle
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5433
DB_SSLMODE=require  # Required for AWS RDS
```

## Security Notes

- **Never commit `.env`, `.env.dev`, or `.env.prod` files**
- **Production**: Set `DJANGO_SECRET_KEY` to a cryptographically secure random string
- **Production**: Set `DEBUG=False`
- **Production**: Restrict `ALLOWED_HOSTS` to your actual domain
- **Production**: Use shorter JWT token lifetimes

## Testing Connection

```bash
# Test database connection
python manage.py dbshell

# Or via health endpoint
curl http://localhost:8000/api/health/
```

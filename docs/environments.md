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
- **Database**: AWS RDS (PostgreSQL)
- **Host**: `prod.czu4cm6ye26e.us-east-2.rds.amazonaws.com`
- **Database**: `Prod`
- **File**: `.env.prod`
- **Debug**: Disabled
- **Usage**: Live application

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

# Production deployment — async imports, Celery, Redis, S3

Runbook to enable the async CSV imports + case/ticket tracking feature in
production. Split into **one-time infrastructure** (do once on the server) and
**per-deploy** (your existing `./deploy.sh` handles it).

Everything referenced here is already in the repo: deps in `requirements.txt`
(boto3, django-storages, celery, redis), the systemd unit
`deploy/celery-worker.service`, the S3 helper (`api/services/import_storage.py`,
regional-endpoint pinned), and the `deploy.sh` celery-restart hook.

---

## A. One-time infrastructure (run once on the EC2 box)

### 1. Redis (Celery broker) — same EC2 to start
```bash
sudo apt-get update && sudo apt-get install -y redis-server
# /etc/redis/redis.conf: ensure  bind 127.0.0.1 ::1  ; add:
#   maxmemory 128mb
#   maxmemory-policy noeviction
sudo systemctl enable --now redis-server
redis-cli ping        # -> PONG
```
Point `CELERY_BROKER_URL` at ElastiCache later with no code change; the default
is `redis://127.0.0.1`.

### 2. Celery worker (systemd)
```bash
sudo cp ~/backend/deploy/celery-worker.service /etc/systemd/system/celery-worker.service
# VERIFY the paths/user in the unit match the server before enabling:
#   User=ubuntu
#   WorkingDirectory=/home/ubuntu/backend
#   ExecStart=/home/ubuntu/backend/venv/bin/celery -A backend worker ...
#   EnvironmentFile=/home/ubuntu/backend/.env
# NOTE: if the server's virtualenv is ./.venv (not ./venv), edit the ExecStart path.
sudo systemctl daemon-reload
sudo systemctl enable --now celery-worker
sudo systemctl status celery-worker      # -> active (running)
```
After this, `deploy.sh` restarts the worker automatically on every deploy (its
`if systemctl list-unit-files | grep celery-worker` block).

### 3. S3 bucket
- Create a **General purpose** bucket, e.g. `carecircle-prod-uploads`, in your
  region (match dev's `us-east-2` unless you prefer otherwise).
- **Block all public access: ON.** Uploads use short-lived presigned PUT URLs
  and reads are server-side with the instance role — no public access needed.
- **CORS** — the browser PUTs directly to S3, so allow PUT from the prod origin:
```json
[
  {
    "AllowedMethods": ["PUT", "GET"],
    "AllowedOrigins": ["https://www.carecircleinternal.com", "https://carecircleinternal.com"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

### 4. IAM (prefer the EC2 instance role over keys)
Attach to the instance role so no keys live in `.env`:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::carecircle-prod-uploads",
      "arn:aws:s3:::carecircle-prod-uploads/*"
    ]
  }]
}
```

### 5. Prod env (`~/backend/.env`)
```
AWS_STORAGE_BUCKET_NAME=carecircle-prod-uploads
AWS_S3_REGION_NAME=us-east-2
# No AWS keys needed when using the instance role.
# CELERY_BROKER_URL / CELERY_RESULT_BACKEND default to redis://127.0.0.1 -- leave unset.
```
Notes:
- `DJANGO_USE_S3` auto-turns **on** when a bucket is set and `DEBUG=False`
  (prod) — no need to set it. (Falls back to filesystem otherwise.)
- With S3 on, it becomes the **default file storage** (imports under `imports/`,
  media under `media/`). If you want to scope S3 to imports only for now, that's
  a separate change — ask before enabling.
- The S3 client pins the **regional endpoint** (`https://s3.<region>.amazonaws.com`),
  so `AWS_S3_REGION_NAME` MUST match the bucket's region or presigned PUTs will
  307-redirect and fail.

---

## B. Per-deploy — your existing `./deploy.sh`
No change required. It already:
- `git pull` + `pip install -r requirements.txt` (celery/redis/boto3/django-storages),
- `python manage.py migrate` (applies `0111`, `0112`, `0113`),
- `collectstatic`,
- restarts **gunicorn**, **nginx**, and **celery-worker** (when the unit is installed).

So after the one-time setup, future releases are just `./deploy.sh`.

---

## C. Frontend
This feature includes frontend changes (Activity Log page + nav, Case history,
reworked Import UI). Deploy the frontend the usual way (Vite build → host); the
API base URL must point at the prod backend.

---

## D. Post-deploy smoke test
```bash
# on the server
systemctl status celery-worker redis-server
journalctl -u celery-worker -f            # or: tail -f ~/backend/celery-worker.log
```
Then in the app (as a manager):
- **Settings → Import Data** → select **Cases** → upload a small Cases CSV →
  watch upload + processing progress complete.
- **Settings → Import Activity** → confirm the run + its actions appear.
- **Activity Log** (left nav) → confirm cross-client events appear.
- Open a member → **Cases** tab → expand an internal-service case → **Case history**.

---

## E. Data backfill (optional, one-time)
To populate history for existing clients/cases so the Activity Log and Case
history aren't sparse:
```bash
# all data (writes many TimelineEvent rows; idempotent)
python manage.py backfill_timeline
# or scope to one client first to sanity-check
python manage.py backfill_timeline --client-id <uuid>
```

---

## F. Safeguards already in place
- **Import header check**: every import validates the CSV header against the
  selected export type AND the key columns each importer needs — so a
  wrong-type upload or a Unite Us column rename fails loudly with a clear
  message instead of silently rejecting rows.
- **Ticket dedupe**: `open_ticket` reuses an existing open ticket per
  (type, client, case, reason) — re-imports don't pile up duplicates.
- **Case tracking** fires on all write paths (CSV import, nightly Unite Us sync,
  live extension writes) with source + actor attribution.

---

## G. Rollback
- Stop the worker: `sudo systemctl stop celery-worker`.
- The migrations are additive (new fields/table columns, nullable) — safe to
  leave in place. Async uploads fall back to the synchronous path automatically
  if the S3 bucket env is removed (`async_uploads:false`).

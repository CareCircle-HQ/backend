# Async CSV Imports (S3 + Celery) — setup

The Settings > Import feature uploads the CSV directly to S3 (presigned PUT),
then a Celery worker streams it back and runs the import, writing progress to
the `ImportRun` row that the browser polls. This survives request timeouts and
the agent closing the tab.

## Environment variables (`.env`)
```
# S3 (bucket for import uploads + media). In prod prefer the EC2 instance role
# over keys: set the bucket/region and leave the key vars unset.
AWS_STORAGE_BUCKET_NAME=carecircle-prod-uploads
AWS_S3_REGION_NAME=us-east-1
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   # only if NOT using an instance role
DJANGO_USE_S3=true

# Celery / Redis (same host to start; ElastiCache later = just change the URL)
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1

# Local dev against MinIO (optional): point boto3/storages at the MinIO endpoint
# AWS_S3_ENDPOINT_URL=http://127.0.0.1:9000
```

## Prod (single EC2 — matches deploy.sh)
1. **Redis**
   ```
   sudo apt-get update && sudo apt-get install -y redis-server
   # /etc/redis/redis.conf: bind 127.0.0.1 ::1 ; maxmemory 128mb ; maxmemory-policy noeviction
   sudo systemctl enable --now redis-server
   ```
2. **Celery worker (systemd)**
   ```
   sudo cp deploy/celery-worker.service /etc/systemd/system/celery-worker.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now celery-worker
   sudo systemctl status celery-worker
   ```
   `deploy.sh` restarts `celery-worker` automatically once the unit is installed.
3. **IAM** — the EC2 instance role needs `s3:PutObject`, `s3:GetObject`,
   `s3:HeadObject` on `arn:aws:s3:::<bucket>/imports/*` (and `media/*`).
4. **S3 bucket CORS** — the browser PUTs directly to S3, so allow PUT from the
   app origin:
   ```json
   [
     {
       "AllowedMethods": ["PUT"],
       "AllowedOrigins": ["https://www.carecircleinternal.com", "https://carecircleinternal.com"],
       "AllowedHeaders": ["*"],
       "ExposeHeaders": ["ETag"],
       "MaxAgeSeconds": 3000
     }
   ]
   ```
5. **nginx** — the large body no longer hits nginx (it goes to S3), so the old
   `client_max_body_size` / timeout bumps for the import endpoint are no longer
   required. Only the small JSON presign/start/poll calls pass through.

## Local dev (real bucket or MinIO)
```
# Redis
brew install redis && brew services start redis        # or: redis-server

# MinIO (S3-compatible) — optional if you don't want to use a real bucket
brew install minio/stable/minio && minio server ~/minio-data --console-address :9001
# create a bucket + set CORS to allow PUT from http://localhost:5173

# .env: AWS_STORAGE_BUCKET_NAME=..., DJANGO_USE_S3=true, (AWS_S3_ENDPOINT_URL for MinIO)

# Run the worker (separate terminal)
.venv/bin/celery -A backend worker -l info --concurrency=2
```
If no bucket is configured the UI automatically falls back to the synchronous
upload (`async_uploads:false`).

## Data model
`ImportRun` gained `export_type`, `file_key` (S3 key, kept for history/re-run),
`original_filename`, and `progress_total` (denominator for the % bar;
`processed_count` is the numerator). Status flow: `pending` (created at presign)
-> `running` (worker) -> `completed` / `failed`.

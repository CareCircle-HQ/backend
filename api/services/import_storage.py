"""S3 helpers for the async CSV import flow (presigned direct upload + worker
download).

Import uploads bypass Django's default storage (which prefixes ``media/``) and
live under a dedicated ``imports/`` prefix via a raw boto3 client, so the key we
presign for the browser PUT is byte-for-byte the key the Celery worker reads
back. Credentials come from the EC2 instance role in prod (leave the key envs
unset); a custom ``AWS_S3_ENDPOINT_URL`` lets the same flow run against a local
MinIO in dev.
"""
import os
import tempfile
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

IMPORTS_PREFIX = "imports"
EXPORTS_PREFIX = "exports"
NUTRITION_PREFIX = "nutrition-reviews"


def s3_enabled():
    """True when a bucket is configured, so the async S3 flow is available."""
    return bool(settings.AWS_STORAGE_BUCKET_NAME)


def _bucket():
    return settings.AWS_STORAGE_BUCKET_NAME


def _client():
    kwargs = {
        "region_name": settings.AWS_S3_REGION_NAME,
        "config": Config(signature_version=settings.AWS_S3_SIGNATURE_VERSION),
        # Pin the REGIONAL endpoint (not the global s3.amazonaws.com, which
        # resolves to us-east-1 and 307-redirects PUTs for buckets in other
        # regions -- breaking the presigned upload). A custom endpoint
        # (e.g. MinIO) overrides this.
        "endpoint_url": settings.AWS_S3_ENDPOINT_URL
        or f"https://s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com",
    }
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
    if settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def build_key(filename):
    """A unique, collision-free key under the imports/ prefix that keeps the
    original filename readable (for the history list)."""
    safe = os.path.basename((filename or "upload.csv")).strip().replace(" ", "_")
    if not safe:
        safe = "upload.csv"
    return f"{IMPORTS_PREFIX}/{uuid.uuid4()}/{safe}"


def build_export_key(filename):
    """A unique key under the exports/ prefix for a generated report CSV,
    keeping the readable filename (used as the download name)."""
    safe = os.path.basename((filename or "export.csv")).strip().replace(" ", "_")
    if not safe:
        safe = "export.csv"
    return f"{EXPORTS_PREFIX}/{uuid.uuid4()}/{safe}"


def build_nutrition_key(client_id, filename="nutrition-review.pdf"):
    """A unique key under the nutrition-reviews/ prefix for a signed PDF."""
    safe = os.path.basename((filename or "nutrition-review.pdf")).strip().replace(" ", "_")
    return f"{NUTRITION_PREFIX}/{client_id}/{uuid.uuid4()}/{safe or 'nutrition-review.pdf'}"


def upload_bytes(key, data, *, content_type="application/octet-stream"):
    """Upload raw bytes directly to S3 (server-side), returning the key."""
    _client().put_object(
        Bucket=_bucket(), Key=key, Body=data, ContentType=content_type,
    )
    return key


def presign_put(key, *, content_type="text/csv", expires=900):
    """Short-lived presigned URL the browser PUTs the file directly to."""
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def presign_get(key, *, expires=900, download_name="", inline=False, content_type=""):
    """Short-lived presigned URL to fetch an object. ``download_name`` sets the
    save-as filename; ``inline=True`` opens it in the browser (view) instead of
    forcing a download; ``content_type`` overrides the response Content-Type."""
    params = {"Bucket": _bucket(), "Key": key}
    if download_name:
        disp = "inline" if inline else "attachment"
        params["ResponseContentDisposition"] = f'{disp}; filename="{download_name}"'
    if content_type:
        params["ResponseContentType"] = content_type
    return _client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires,
    )


def object_exists(key):
    """True if the object was actually uploaded (guards enqueue)."""
    try:
        _client().head_object(Bucket=_bucket(), Key=key)
        return True
    except (ClientError, BotoCoreError):
        return False


def download_to_temp(key):
    """Stream the S3 object to a local temp file and return it (opened, at pos
    0). Seekable + on-disk so the importer can pre-count rows and stream without
    holding the (potentially multi-GB) file in memory. Caller must close it and
    unlink ``.name``."""
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    _client().download_fileobj(_bucket(), key, tmp)
    tmp.flush()
    tmp.seek(0)
    return tmp


def delete_object(key):
    """Best-effort delete of an object under the imports/ prefix. Returns True
    when a delete request was issued, False when skipped (no key / no bucket) or
    it failed (logged by the caller). Never raises."""
    if not key or not s3_enabled():
        return False
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
        return True
    except (BotoCoreError, ClientError):
        return False


def upload_fileobj(key, fileobj, *, content_type="text/csv"):
    """Server-side upload of a file-like to S3 under the imports/ prefix.

    The manual-upload flow presigns a URL for the browser to PUT to; the Unite
    Us export automation instead downloads the export server-side and uploads it
    here so it can be processed by the same Celery/import pipeline. Rewinds the
    file first and streams it (no full in-memory read)."""
    try:
        fileobj.seek(0)
    except (AttributeError, OSError, ValueError):
        pass
    _client().upload_fileobj(
        fileobj, _bucket(), key, ExtraArgs={"ContentType": content_type}
    )
    return key

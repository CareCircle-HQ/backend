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


def presign_put(key, *, content_type="text/csv", expires=900):
    """Short-lived presigned URL the browser PUTs the file directly to."""
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
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

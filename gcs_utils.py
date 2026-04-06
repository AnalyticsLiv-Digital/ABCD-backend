"""
GCS upload helper for Creative Studio image storage.
Images are uploaded to the configured bucket and their public URLs are returned.
The bucket must have public read access (allUsers: Storage Object Viewer) or
uniform bucket-level access disabled with per-object ACLs allowed.
"""
import logging
from typing import Optional

from config import settings

_log = logging.getLogger(__name__)
_storage_client = None


def _get_storage_client():
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage
        _storage_client = storage.Client()
    return _storage_client


def _bucket_name() -> str:
    return settings.GCS_BUCKET or settings.GCS_BUCKET


def upload_bytes_to_gcs(data: bytes, blob_name: str, content_type: str = "image/png") -> str:
    """Upload raw bytes to GCS and return the public HTTPS URL.

    Raises RuntimeError if GCS is not configured.
    Raises google.cloud.exceptions.GoogleCloudError on upload failure.
    """
    bucket = _bucket_name()
    if not bucket:
        raise RuntimeError(
            "GCS bucket not configured. Set GCS_BUCKET (or GCS_BUCKET) in .env"
        )
    client = _get_storage_client()
    b = client.bucket(bucket)
    blob = b.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    _log.info("Uploaded %d bytes → gs://%s/%s", len(data), bucket, blob_name)
    return f"https://storage.googleapis.com/{bucket}/{blob_name}"

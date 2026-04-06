import logging

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
    return settings.GCS_IMAGES_BUCKET or settings.GCS_BUCKET


def upload_bytes_to_gcs(data: bytes, blob_name: str, content_type: str = "image/png") -> str:
    """Upload raw bytes to GCS and return the public HTTPS URL."""
    bucket_name = _bucket_name()
    if not bucket_name:
        raise RuntimeError("No GCS bucket configured. Set GCS_IMAGES_BUCKET or GCS_BUCKET.")
    client = _get_storage_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    _log.info("Uploaded %d bytes → gs://%s/%s", len(data), bucket_name, blob_name)
    return f"https://storage.googleapis.com/{bucket_name}/{blob_name}"

"""
GCS helpers for the ViGenAiR engine bucket: folder naming, copies, reads and signed URLs.

The engine is a Cloud Function triggered by every object written to VIGENAIR_BUCKET.
Writing `<folder>/input.<ext>` starts an analysis and writing `<folder>/<ts>-combos/render.json`
starts a render, so writes here are deliberate and few.
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

import google.auth
from google.auth.transport.requests import Request
from google.cloud import storage

from config import settings

_log = logging.getLogger(__name__)
_client: Optional[storage.Client] = None

# Signing goes through IAM (~0.3–0.8 s per URL) when there's no key file, so URLs are
# cached and reused while they still have at least SIGNED_URL_MIN_LEFT_S of validity.
SIGNED_URL_MINUTES = 120
SIGNED_URL_MIN_LEFT_S = 30 * 60
_signed_cache: Dict[tuple, Tuple[str, float]] = {}
_signed_lock = threading.Lock()

# Characters ViGenAiR strips from video names (upstream ui/src/string-util.ts)
_UNSAFE_NAME_CHARS = re.compile(r'[#/\[\]*?:"<>|]')
VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "mpeg", "mpg", "flv", "wmv", "3gp", "ps", "m2p"}


class VigenairConfigError(RuntimeError):
    """The backend isn't configured for Video Variants (e.g. VIGENAIR_BUCKET unset)."""


def client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def engine_bucket() -> str:
    if not settings.VIGENAIR_BUCKET:
        raise VigenairConfigError("VIGENAIR_BUCKET is not configured")
    return settings.VIGENAIR_BUCKET


def parse_gs_uri(uri: str) -> Tuple[str, str]:
    if not uri or not uri.startswith("gs://"):
        raise ValueError(f"Not a gs:// URI: {uri!r}")
    bucket, _, name = uri[5:].partition("/")
    if not bucket or not name:
        raise ValueError(f"Not a gs:// object URI: {uri!r}")
    return bucket, name


def make_folder_name(original_filename: str, user_ref: str, transcription: str = "g") -> str:
    """
    `<video_name>--<transcription>--<timestamp_ms>--<encoded_user_id>` (guide §2.5).
    The engine parses it with rsplit('--', 3), so only the user id must be free of '--'.
    """
    stem = os.path.splitext(os.path.basename(original_filename or "video"))[0]
    stem = _UNSAFE_NAME_CHARS.sub("", stem)
    stem = re.sub(r"\s+", "_", stem.strip())
    stem = re.sub(r"-{2,}", "-", stem).strip("-_")[:60] or "video"
    user = re.sub(r"[^A-Za-z0-9_.@-]", "", user_ref or "user").replace("--", "-") or "user"
    return f"{stem}--{transcription}--{int(time.time() * 1000)}--{user}"


def blob(bucket: str, name: str) -> storage.Blob:
    return client().bucket(bucket).blob(name)


def get_blob(bucket: str, name: str) -> Optional[storage.Blob]:
    """Blob with metadata loaded, or None if it doesn't exist."""
    return client().bucket(bucket).get_blob(name)


def list_names(bucket: str, prefix: str, top_level_only: bool = False) -> List[str]:
    """Object names under prefix; top_level_only skips sub-folders (analysis_chunks/ etc.)."""
    if top_level_only:
        return [b.name for b in client().list_blobs(bucket, prefix=prefix, delimiter="/")]
    return [b.name for b in client().list_blobs(bucket, prefix=prefix)]


def read_text(bucket: str, name: str) -> Optional[str]:
    b = get_blob(bucket, name)
    return b.download_as_text(encoding="utf-8") if b else None


def read_json(bucket: str, name: str) -> Optional[Any]:
    text = read_text(bucket, name)
    return json.loads(text) if text is not None else None


def write_json(bucket: str, name: str, obj: Any) -> None:
    blob(bucket, name).upload_from_string(
        json.dumps(obj, ensure_ascii=False, indent=2), content_type="application/json"
    )


def copy(src_bucket: str, src_name: str, dst_bucket: str, dst_name: str) -> None:
    """Server-side copy; rewrite() in a loop so large or cross-region copies don't time out."""
    src = client().bucket(src_bucket).blob(src_name)
    dst = client().bucket(dst_bucket).blob(dst_name)
    token, _, _ = dst.rewrite(src)
    while token is not None:
        token, _, _ = dst.rewrite(src, token=token)


def delete_prefix(bucket: str, prefix: str) -> int:
    blobs = list(client().list_blobs(bucket, prefix=prefix))
    for b in blobs:
        try:
            b.delete()
        except Exception as exc:  # already gone, etc.
            _log.warning("Could not delete gs://%s/%s: %s", bucket, b.name, exc)
    return len(blobs)


def signed_url(bucket: str, name: str, minutes: int = SIGNED_URL_MINUTES, download_filename: Optional[str] = None) -> str:
    """
    V4 signed GET URL (cached). Works with a key file, impersonated ADC, and on Cloud Run
    (no private key there: signing goes through IAM signBlob as VIGENAIR_SIGNER_SA).
    """
    key = (bucket, name, download_filename)
    now = time.time()
    with _signed_lock:
        hit = _signed_cache.get(key)
    if hit and hit[1] - now > min(SIGNED_URL_MIN_LEFT_S, minutes * 30):
        return hit[0]
    url = _sign(bucket, name, minutes, download_filename)
    with _signed_lock:
        if len(_signed_cache) > 5000:
            _signed_cache.clear()
        _signed_cache[key] = (url, now + minutes * 60)
    return url


def sign_many(items: Iterable[Tuple[str, str, Optional[str]]]) -> List[str]:
    """Sign (bucket, name, download_filename) items in parallel, same order."""
    items = list(items)
    if len(items) <= 1:
        return [signed_url(b, n, download_filename=d) for b, n, d in items]
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return list(pool.map(lambda it: signed_url(it[0], it[1], download_filename=it[2]), items))


def signed_upload_url(bucket: str, name: str, content_type: str, minutes: int = 60) -> str:
    """
    V4 signed PUT URL so the browser uploads straight to GCS (Cloud Run rejects request
    bodies over 32 MB). The browser must send the same Content-Type; the bucket needs a
    CORS rule allowing PUT from the app's origins. Not cached: one URL per upload.
    """
    return _sign(bucket, name, minutes, None, method="PUT", content_type=content_type)


def _sign(bucket: str, name: str, minutes: int, download_filename: Optional[str],
          method: str = "GET", content_type: Optional[str] = None) -> str:
    kwargs: dict = {"version": "v4", "expiration": timedelta(minutes=minutes), "method": method}
    if content_type:
        kwargs["content_type"] = content_type
    if download_filename:
        safe = download_filename.replace('"', "")
        kwargs["response_disposition"] = f'attachment; filename="{safe}"'
    b = blob(bucket, name)
    try:
        return b.generate_signed_url(**kwargs)
    except (AttributeError, TypeError, ValueError):
        # Credentials can't sign locally (Cloud Run / compute / user ADC) → sign via IAM.
        creds = _token_credentials()
        signer = settings.VIGENAIR_SIGNER_SA or getattr(creds, "service_account_email", None)
        if not signer or signer == "default":
            raise VigenairConfigError(
                "Cannot sign URLs: set VIGENAIR_SIGNER_SA or use service-account credentials"
            )
        return b.generate_signed_url(service_account_email=signer, access_token=creds.token, **kwargs)


_token_creds = None


def _token_credentials():
    """ADC loaded once and refreshed only when the token expires (not on every URL)."""
    global _token_creds
    with _signed_lock:
        if _token_creds is None:
            _token_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _token_creds.valid:
            _token_creds.refresh(Request())
        return _token_creds


def entity_to_blob_name(entity_url: str, bucket: str) -> Optional[str]:
    """combos.json stores outputs as https://storage.mtls.cloud.google.com/<bucket>/<quoted path>."""
    marker = f"/{bucket}/"
    i = entity_url.find(marker)
    return unquote(entity_url[i + len(marker):]) if i >= 0 else None

"""
Creative Studio – Image Enhancement Jobs API

Async callback pattern (no 504):
  1. POST /image-jobs         → DB record, fire background task, return job_id in < 200ms
  2. Background task          → upload original to GCS, POST to n8n with callback_url (30s ack timeout)
  3. n8n workflow             → "Respond to Webhook" immediately, does processing, POSTs results back
  4. POST /image-jobs/{id}/complete → receives results, uploads to GCS, marks completed
  5. GET  /image-jobs/{id}    → poll until status = completed | failed
  6. GET  /image-jobs         → list user's history
"""
import base64
import hashlib
import hmac
import io
import logging
from typing import List, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile

from config import settings
from gcs_utils import upload_bytes_to_gcs
from user_repository import check_and_increment_service_usage
from image_job_repository import (
    create_image_job_record,
    get_image_job,
    list_image_jobs,
    set_image_job_completed,
    set_image_job_failed,
    set_image_job_processing,
    update_original_url,
)
from routers.auth import get_current_user

router = APIRouter(prefix="/image-jobs", tags=["image-jobs"])
_log = logging.getLogger(__name__)


# ── Access check ──────────────────────────────────────────────────────────────

def _check_access(user: dict) -> None:
    """
    Allow access if the user has 'creative_studio' in allowed_services.
    Admin users always get access, even if allowed_services is not yet stored
    in their MongoDB document (backwards-compatible for existing users).
    """
    roles = user.get("roles") or []
    is_admin = "admin" in roles
    if is_admin:
        return
    services = user.get("allowed_services") or ["abcd_analyzer"]
    if "creative_studio" not in services:
        raise HTTPException(
            status_code=403,
            detail="Your account does not have access to Creative Studio. Contact an admin.",
        )


def _to_response(doc: dict) -> dict:
    return {
        "job_id":            doc["job_id"],
        "status":            doc["status"],
        "created_at":        doc["created_at"],
        "completed_at":      doc.get("completed_at"),
        "prompt":            doc.get("prompt"),
        "original_filename": doc.get("original_filename"),
        "original_url":      doc.get("original_url"),
        "result_urls":       doc.get("result_urls") or [],
        "error":             doc.get("error"),
    }


# ── Background worker ─────────────────────────────────────────────────────────

def _process(
    job_id: str,
    image_data: bytes,
    content_type: str,
    filename: str,
    prompt: str,
    callback_url: str,
) -> None:
    """
    Runs in FastAPI's thread pool — response has already been sent.

    Steps:
      1. Upload original image to GCS
      2. POST to n8n with multipart form including callback_url + secret (30s ack timeout)
         n8n must respond immediately (via "Respond to Webhook" node) — actual processing
         happens asynchronously inside n8n, which then POSTs results to callback_url.
    """
    # 1. Upload original to GCS
    ext = (content_type.split("/")[-1].split(";")[0] or "jpg")[:10]
    try:
        original_url = upload_bytes_to_gcs(
            image_data, f"image_jobs/{job_id}/original.{ext}", content_type
        )
        update_original_url(job_id, original_url)
    except Exception as exc:
        _log.warning("Original GCS upload failed for job %s (non-fatal): %s", job_id, exc)

    # 2. Call n8n (expect immediate ack, not final result)
    if not settings.N8N_IMAGE_WEBHOOK_URL:
        _log.warning("N8N_IMAGE_WEBHOOK_URL not configured; job %s failed", job_id)
        set_image_job_failed(job_id, "Image processing service not configured on server.")
        return

    try:
        import requests as _req
        from requests.exceptions import ReadTimeout, ConnectionError as ReqConnError

        _log.info("Sending job %s to n8n…", job_id)

        parsed = urlparse(settings.N8N_IMAGE_WEBHOOK_URL)
        existing_qs = parse_qs(parsed.query)
        existing_qs["callback_url"] = [callback_url]
        existing_qs["job_id"]       = [job_id]
        webhook_url = urlunparse(parsed._replace(query=urlencode(existing_qs, doseq=True)))

        form_data = {
            "job_id":          job_id,
            "callback_url":    callback_url,
            "callback_secret": settings.N8N_CALLBACK_SECRET,
        }
        if prompt:
            form_data["prompt"] = prompt

        _log.info("n8n webhook URL: %s", webhook_url)

        try:
            resp = _req.post(
                webhook_url,
                files={"image": (filename, io.BytesIO(image_data), content_type)},
                data=form_data,
                timeout=120,  # increased: n8n may respond slowly if not using "Respond to Webhook"
            )
            if resp.ok:
                _log.info("n8n ack for job %s (HTTP %s) — marking processing", job_id, resp.status_code)
                set_image_job_processing(job_id)
            else:
                _log.error("n8n returned HTTP %s for job %s: %s", resp.status_code, job_id, resp.text[:200])
                set_image_job_failed(job_id, f"Processing service returned HTTP {resp.status_code}")

        except ReadTimeout:
            # n8n received the request but took > 120s to ack.
            # The workflow is almost certainly still running and will POST the callback when done.
            # Leave the job as "processing" so the frontend keeps polling.
            _log.warning(
                "n8n read timeout for job %s (>120s) — workflow still running, waiting for callback",
                job_id,
            )
            set_image_job_processing(job_id)

        except ReqConnError as exc:
            # Could not connect at all — n8n is down or unreachable
            _log.error("Cannot connect to n8n for job %s: %s", job_id, exc)
            set_image_job_failed(job_id, "Image processing service is unreachable. Try again later.")

    except Exception as exc:
        _log.error("Unexpected error in background task for job %s: %s", job_id, exc)
        set_image_job_failed(job_id, str(exc))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
async def create_image_job(
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Submit an image for AI enhancement.

    Returns in < 200ms (only a MongoDB write + background task scheduling).
    Poll GET /image-jobs/{id} every 5 s until status = completed | failed.
    """
    _check_access(current_user)

    if not check_and_increment_service_usage(current_user, "creative_studio"):
        raise HTTPException(
            status_code=429,
            detail="Monthly usage limit reached for Creative Studio. Contact an admin to increase your limit.",
        )

    image_data = await image.read()
    if len(image_data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 20 MB)")

    content_type = (image.content_type or "image/jpeg").split(";")[0].strip()
    safe_filename = image.filename or "image.jpg"

    job_id = create_image_job_record(
        user_email=current_user["email"],
        prompt=prompt,
        original_filename=safe_filename,
    )

    # Build callback URL. Prefer BACKEND_PUBLIC_URL (required for local dev since
    # n8n is external and cannot reach localhost). Falls back to request.base_url
    # for cloud deployments where the ingress URL is already correct.
    if settings.BACKEND_PUBLIC_URL:
        base = settings.BACKEND_PUBLIC_URL.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    callback_url = f"{base}/image-jobs/{job_id}/complete"

    background_tasks.add_task(
        _process,
        job_id,
        image_data,
        content_type,
        safe_filename,
        prompt or "",
        callback_url,
    )

    doc = get_image_job(job_id, current_user["email"])
    return _to_response(doc)


@router.post("/{job_id}/complete")
async def image_job_complete(job_id: str, request: Request):
    """
    Callback endpoint called by n8n after processing is done.

    Expected JSON body (sent by n8n HTTP Request node):
    {
      "callback_secret": "<secret>",
      "images": [
        {"data": "<base64>", "content_type": "image/png"},
        ...
      ]
    }

    Or for a single image:
    {
      "callback_secret": "<secret>",
      "image": "<base64>",
      "content_type": "image/png"
    }
    """
    body = await request.json()

    # Verify secret
    received_secret = body.get("callback_secret", "")
    expected_secret = settings.N8N_CALLBACK_SECRET
    if not hmac.compare_digest(received_secret, expected_secret):
        _log.warning("Invalid callback_secret for job %s", job_id)
        raise HTTPException(status_code=403, detail="Invalid callback secret")

    # Normalise to list of (bytes, content_type)
    result_pairs: List[tuple] = []

    images_list = body.get("images")
    if images_list and isinstance(images_list, list):
        for item in images_list:
            raw_b64 = item.get("data", "")
            item_ct  = item.get("content_type", "image/png")
            try:
                result_pairs.append((base64.b64decode(raw_b64), item_ct))
            except Exception:
                pass
    else:
        raw   = body.get("image") or body.get("data") or body.get("result") or body.get("output")
        img_ct = body.get("content_type", "image/png")
        if raw is None:
            _log.error("Callback for job %s had no image data", job_id)
            set_image_job_failed(job_id, "Callback contained no image data")
            return {"ok": False}

        if isinstance(raw, str) and raw.startswith("http"):
            import requests as _req
            img_resp = _req.get(raw, timeout=60)
            dl_ct = img_resp.headers.get("content-type", "image/png").split(";")[0].strip()
            result_pairs.append((img_resp.content, dl_ct))
        elif isinstance(raw, str):
            cleaned = raw.split(",", 1)[-1] if "," in raw else raw
            result_pairs.append((base64.b64decode(cleaned), img_ct))
        else:
            set_image_job_failed(job_id, "Unexpected image format in callback")
            return {"ok": False}

    if not result_pairs:
        set_image_job_failed(job_id, "Callback contained no usable images")
        return {"ok": False}

    # Upload results to GCS
    result_urls: List[str] = []
    for i, (img_bytes, img_ct) in enumerate(result_pairs):
        r_ext = (img_ct.split("/")[-1].split(";")[0] or "png")[:10]
        blob  = f"image_jobs/{job_id}/result_{i}.{r_ext}"
        try:
            url = upload_bytes_to_gcs(img_bytes, blob, img_ct)
            result_urls.append(url)
        except Exception as exc:
            _log.error("GCS upload failed for result %d of job %s: %s", i, job_id, exc)

    if result_urls:
        set_image_job_completed(job_id, result_urls)
        _log.info("Job %s completed via callback — %d result(s)", job_id, len(result_urls))
        return {"ok": True, "result_count": len(result_urls)}
    else:
        set_image_job_failed(job_id, "Failed to store result images to cloud storage")
        return {"ok": False}


@router.get("")
async def list_image_jobs_endpoint(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """List the current user's image enhancement history (most recent first)."""
    _check_access(current_user)
    docs = list_image_jobs(current_user["email"], limit=min(limit, 100))
    return [_to_response(d) for d in docs]


@router.get("/{job_id}/results/{image_index}/download")
async def download_image_result(
    job_id: str,
    image_index: int,
    current_user: dict = Depends(get_current_user),
):
    """Proxy-download a result image from GCS so the browser saves it instead of opening it."""
    import requests as _req
    from fastapi.responses import StreamingResponse

    _check_access(current_user)
    doc = get_image_job(job_id, current_user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")

    result_urls = doc.get("result_urls") or []
    if image_index >= len(result_urls):
        raise HTTPException(404, "Image index out of range")

    gcs_url = result_urls[image_index]
    try:
        r = _req.get(gcs_url, timeout=60, stream=True)
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch image from storage: {exc}")

    ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
    ext = ct.split("/")[-1] or "png"
    base = (doc.get("original_filename") or "image").rsplit(".", 1)[0]
    suffix = f"_enhanced_{image_index + 1}" if len(result_urls) > 1 else "_enhanced"
    filename = f"{base}{suffix}.{ext}"

    def _stream():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type=ct,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{job_id}")
async def get_image_job_endpoint(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a single image job. Poll every 5 s until status != pending."""
    _check_access(current_user)
    doc = get_image_job(job_id, current_user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")
    return _to_response(doc)

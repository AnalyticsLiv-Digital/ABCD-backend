"""
Creative Studio – Image Enhancement Jobs API

The n8n webhook is called synchronously from a background thread (long timeout),
so the browser never directly touches n8n.  This fixes both CORS and 504 issues
without requiring any changes to the n8n workflow.

Flow:
  1. POST /image-jobs      → create DB record, schedule background work, return job_id IN < 200ms
  2. Background thread     → upload original to GCS, call n8n (5 min timeout), store results
  3. GET  /image-jobs/{id} → poll until status = completed | failed
  4. GET  /image-jobs      → list user's history
"""
import base64
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile

from config import settings
from gcs_utils import upload_bytes_to_gcs
from image_job_repository import (
    create_image_job_record,
    get_image_job,
    list_image_jobs,
    set_image_job_completed,
    set_image_job_failed,
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
    # Admins always have full access
    if is_admin:
        return
    # For non-admins, check the explicit services list
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
) -> None:
    """
    Runs entirely in FastAPI's thread pool — the HTTP response has already
    been sent to the browser before this function starts.

    Steps:
      1. Upload original image to GCS (for history thumbnails)
      2. POST to n8n with multipart form — same format the browser used to send
      3. Parse n8n response (binary image or JSON envelope)
      4. Upload result(s) to GCS
      5. Mark job completed / failed in MongoDB
    """
    # 1. Upload original to GCS (non-blocking from browser's perspective)
    ext = (content_type.split("/")[-1].split(";")[0] or "jpg")[:10]
    try:
        original_url = upload_bytes_to_gcs(
            image_data, f"image_jobs/{job_id}/original.{ext}", content_type
        )
        update_original_url(job_id, original_url)
    except Exception as exc:
        _log.warning("Original GCS upload failed for job %s (non-fatal): %s", job_id, exc)

    # 2. Call n8n
    if not settings.N8N_IMAGE_WEBHOOK_URL:
        _log.warning("N8N_IMAGE_WEBHOOK_URL not configured; job %s failed", job_id)
        set_image_job_failed(job_id, "Image processing service not configured on server.")
        return

    try:
        import requests as _req

        _log.info("Sending job %s to n8n (timeout=300s)…", job_id)
        resp = _req.post(
            settings.N8N_IMAGE_WEBHOOK_URL,
            files={"image": (filename, io.BytesIO(image_data), content_type)},
            data={"prompt": prompt} if prompt else {},
            timeout=300,   # 5-minute ceiling
        )

        if not resp.ok:
            set_image_job_failed(job_id, f"Processing service returned HTTP {resp.status_code}")
            return

        # 3. Parse response — mirrors original browser-side logic
        resp_ct = resp.headers.get("content-type", "")
        result_pairs: List[tuple] = []   # [(bytes, content_type_str), …]

        if resp_ct.startswith("image/"):
            ct = resp_ct.split(";")[0].strip()
            result_pairs.append((resp.content, ct))
        else:
            try:
                body = resp.json()
            except Exception:
                set_image_job_failed(job_id, "Unrecognised response from processing service")
                return

            images_list = body.get("images")
            if images_list and isinstance(images_list, list):
                # Multi-image JSON: [{"data": "base64", "content_type": "image/png"}, …]
                for item in images_list:
                    raw_b64 = item.get("data", "")
                    item_ct  = item.get("content_type", "image/png")
                    try:
                        result_pairs.append((base64.b64decode(raw_b64), item_ct))
                    except Exception:
                        pass
            else:
                # Single-image JSON: image / data / result / output key
                raw = (
                    body.get("image")
                    or body.get("data")
                    or body.get("result")
                    or body.get("output")
                )
                if raw is None:
                    set_image_job_failed(job_id, "No image found in processing service response")
                    return

                if isinstance(raw, str) and raw.startswith("http"):
                    img_resp = _req.get(raw, timeout=60)
                    dl_ct = img_resp.headers.get("content-type", "image/png").split(";")[0].strip()
                    result_pairs.append((img_resp.content, dl_ct))
                elif isinstance(raw, str):
                    cleaned = raw.split(",", 1)[-1] if "," in raw else raw
                    result_pairs.append((base64.b64decode(cleaned), "image/png"))
                else:
                    set_image_job_failed(job_id, "Unexpected image format in response")
                    return

        if not result_pairs:
            set_image_job_failed(job_id, "Processing service returned no usable images")
            return

        # 4. Upload results to GCS
        result_urls: List[str] = []
        for i, (img_bytes, img_ct) in enumerate(result_pairs):
            r_ext = (img_ct.split("/")[-1].split(";")[0] or "png")[:10]
            blob  = f"image_jobs/{job_id}/result_{i}.{r_ext}"
            try:
                url = upload_bytes_to_gcs(img_bytes, blob, img_ct)
                result_urls.append(url)
            except Exception as exc:
                _log.error("GCS upload failed for result %d of job %s: %s", i, job_id, exc)

        # 5. Update status
        if result_urls:
            set_image_job_completed(job_id, result_urls)
            _log.info("Job %s completed — %d result(s)", job_id, len(result_urls))
        else:
            set_image_job_failed(job_id, "Failed to store result images to cloud storage")

    except Exception as exc:
        _log.error("Unexpected error in job %s: %s", job_id, exc)
        set_image_job_failed(job_id, str(exc))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
async def create_image_job(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Submit an image for AI enhancement.

    The endpoint returns in < 200ms — only a MongoDB write happens here.
    All GCS uploads and the n8n call run in a background thread after the
    response is already sent, so there is no browser-side timeout or CORS.

    Poll GET /image-jobs/{id} every 5 s until status = completed | failed.
    """
    _check_access(current_user)

    image_data = await image.read()
    if len(image_data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 20 MB)")

    content_type = (image.content_type or "image/jpeg").split(";")[0].strip()
    safe_filename = image.filename or "image.jpg"

    # Only DB write happens in the request handler — guaranteed fast return
    job_id = create_image_job_record(
        user_email=current_user["email"],
        prompt=prompt,
        original_filename=safe_filename,
    )

    # Everything else (GCS + n8n) runs after the response is sent
    background_tasks.add_task(
        _process,
        job_id,
        image_data,
        content_type,
        safe_filename,
        prompt or "",
    )

    doc = get_image_job(job_id, current_user["email"])
    return _to_response(doc)


@router.get("")
async def list_image_jobs_endpoint(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """List the current user's image enhancement history (most recent first)."""
    _check_access(current_user)
    docs = list_image_jobs(current_user["email"], limit=min(limit, 100))
    return [_to_response(d) for d in docs]


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

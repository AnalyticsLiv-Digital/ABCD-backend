"""
Creative Resize – Image Resizing Jobs API

Async callback pattern (same as image-jobs):
  1. POST /resize-jobs         → DB record, fire background task, return job_id < 200ms
  2. Background task           → upload original to GCS, POST to n8n with resize params + callback_url
  3. n8n workflow              → responds immediately, processes async, POSTs result back
  4. POST /resize-jobs/{id}/complete → receives resized image(s), uploads to GCS, marks completed
  5. GET  /resize-jobs/{id}    → poll until status = completed | failed
  6. GET  /resize-jobs         → list user's resize history

Payload sent TO n8n (multipart/form-data):
  - image          : original image file
  - job_id         : UUID string
  - callback_url   : full URL to POST results back to
  - callback_secret: shared secret for verification
  - target_format  : preset id (e.g. "instagram_square") or "custom"
  - target_width   : integer px
  - target_height  : integer px
  - quality        : "draft" | "standard" | "high" | "lossless"
  - fit_mode       : "cover" | "contain" | "fill"

Callback payload expected FROM n8n (JSON):
  { "callback_secret": "...", "images": [{"data": "<base64>", "content_type": "image/png"}] }
  OR { "callback_secret": "...", "image": "<base64 or URL>", "content_type": "image/png" }
"""
import base64
import hmac
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile

from config import settings
from gcs_utils import upload_bytes_to_gcs
from resize_job_repository import (
    create_resize_job_record,
    get_resize_job,
    list_resize_jobs,
    set_resize_job_completed,
    set_resize_job_failed,
    update_resize_original_url,
)
from routers.auth import get_current_user

router = APIRouter(prefix="/resize-jobs", tags=["resize-jobs"])
_log = logging.getLogger(__name__)


# ── Access check ──────────────────────────────────────────────────────────────

def _check_access(user: dict) -> None:
    """Allow access if user has 'creative_resize' in allowed_services or is admin."""
    if "admin" in (user.get("roles") or []) or user.get("is_admin"):
        return
    services = user.get("allowed_services") or []
    if "creative_resize" not in services:
        raise HTTPException(
            status_code=403,
            detail="Your account does not have access to Creative Resize. Contact an admin.",
        )


def _to_response(doc: dict) -> dict:
    return {
        "job_id":            doc["job_id"],
        "status":            doc["status"],
        "created_at":        doc["created_at"],
        "completed_at":      doc.get("completed_at"),
        "original_filename": doc.get("original_filename"),
        "original_url":      doc.get("original_url"),
        "target_format":     doc.get("target_format"),
        "target_width":      doc.get("target_width"),
        "target_height":     doc.get("target_height"),
        "quality":           doc.get("quality"),
        "fit_mode":          doc.get("fit_mode"),
        "result_urls":       doc.get("result_urls") or [],
        "error":             doc.get("error"),
    }


# ── Background worker ─────────────────────────────────────────────────────────

def _process(
    job_id: str,
    image_data: bytes,
    content_type: str,
    filename: str,
    callback_url: str,
    target_format: str,
    target_width: Optional[int],
    target_height: Optional[int],
    quality: str,
    fit_mode: str,
) -> None:
    """Upload original to GCS then POST to n8n with resize parameters."""
    # 1. Upload original to GCS
    ext = (content_type.split("/")[-1].split(";")[0] or "jpg")[:10]
    try:
        original_url = upload_bytes_to_gcs(
            image_data, f"resize_jobs/{job_id}/original.{ext}", content_type
        )
        update_resize_original_url(job_id, original_url)
    except Exception as exc:
        _log.warning("Original GCS upload failed for resize job %s (non-fatal): %s", job_id, exc)

    # 2. Call n8n
    if not settings.N8N_RESIZE_WEBHOOK_URL:
        _log.warning("N8N_RESIZE_WEBHOOK_URL not configured; resize job %s failed", job_id)
        set_resize_job_failed(job_id, "Image resize service not configured on server.")
        return

    try:
        import requests as _req

        _log.info("Sending resize job %s to n8n (ack timeout=30s)…", job_id)
        form_data = {
            "job_id":          job_id,
            "callback_url":    callback_url,
            "callback_secret": settings.N8N_CALLBACK_SECRET,
            "target_format":   target_format,
            "quality":         quality,
            "fit_mode":        fit_mode,
        }
        if target_width is not None:
            form_data["target_width"] = str(target_width)
        if target_height is not None:
            form_data["target_height"] = str(target_height)

        resp = _req.post(
            settings.N8N_RESIZE_WEBHOOK_URL,
            files={"image": (filename, io.BytesIO(image_data), content_type)},
            data=form_data,
            timeout=30,
        )

        if resp.ok:
            _log.info("n8n ack received for resize job %s (status %s)", job_id, resp.status_code)
        else:
            _log.error("n8n returned HTTP %s for resize job %s", resp.status_code, job_id)
            set_resize_job_failed(job_id, f"Processing service returned HTTP {resp.status_code}")

    except Exception as exc:
        _log.error("Failed to reach n8n for resize job %s: %s", job_id, exc)
        set_resize_job_failed(job_id, str(exc))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
async def create_resize_job(
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    target_format: str = Form("custom"),
    target_width: Optional[int] = Form(None),
    target_height: Optional[int] = Form(None),
    quality: str = Form("standard"),
    fit_mode: str = Form("cover"),
    current_user: dict = Depends(get_current_user),
):
    """
    Submit an image for resizing/adaptation.

    target_format: preset id (instagram_square, stories_reels, youtube_thumbnail,
                   linkedin, twitter_post, facebook_post, display_leaderboard,
                   display_medium, instagram_portrait) or "custom"
    target_width / target_height: pixel dimensions (required for custom, used for all)
    quality: draft | standard | high | lossless
    fit_mode: cover (crop to fill) | contain (letterbox) | fill (stretch)
    """
    _check_access(current_user)

    # Validate quality and fit_mode
    if quality not in ("draft", "standard", "high", "lossless"):
        quality = "standard"
    if fit_mode not in ("cover", "contain", "fill"):
        fit_mode = "cover"

    image_data = await image.read()
    if len(image_data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 20 MB)")

    content_type = (image.content_type or "image/jpeg").split(";")[0].strip()
    safe_filename = image.filename or "image.jpg"

    job_id = create_resize_job_record(
        user_email=current_user["email"],
        original_filename=safe_filename,
        target_format=target_format,
        target_width=target_width,
        target_height=target_height,
        quality=quality,
        fit_mode=fit_mode,
    )

    if settings.BACKEND_PUBLIC_URL:
        base = settings.BACKEND_PUBLIC_URL.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    callback_url = f"{base}/resize-jobs/{job_id}/complete"

    background_tasks.add_task(
        _process,
        job_id,
        image_data,
        content_type,
        safe_filename,
        callback_url,
        target_format,
        target_width,
        target_height,
        quality,
        fit_mode,
    )

    doc = get_resize_job(job_id, current_user["email"])
    return _to_response(doc)


@router.post("/{job_id}/complete")
async def resize_job_complete(job_id: str, request: Request):
    """
    Callback endpoint called by n8n after resizing is done.
    Accepts same payload format as /image-jobs/{id}/complete.
    """
    body = await request.json()

    received_secret = body.get("callback_secret", "")
    if not hmac.compare_digest(received_secret, settings.N8N_CALLBACK_SECRET):
        _log.warning("Invalid callback_secret for resize job %s", job_id)
        raise HTTPException(status_code=403, detail="Invalid callback secret")

    result_pairs: List[tuple] = []

    images_list = body.get("images")
    if images_list and isinstance(images_list, list):
        for item in images_list:
            raw_b64 = item.get("data", "")
            item_ct = item.get("content_type", "image/png")
            try:
                result_pairs.append((base64.b64decode(raw_b64), item_ct))
            except Exception:
                pass
    else:
        raw = body.get("image") or body.get("data") or body.get("result") or body.get("output")
        img_ct = body.get("content_type", "image/png")
        if raw is None:
            _log.error("Callback for resize job %s had no image data", job_id)
            set_resize_job_failed(job_id, "Callback contained no image data")
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
            set_resize_job_failed(job_id, "Unexpected image format in callback")
            return {"ok": False}

    if not result_pairs:
        set_resize_job_failed(job_id, "Callback contained no usable images")
        return {"ok": False}

    result_urls: List[str] = []
    for i, (img_bytes, img_ct) in enumerate(result_pairs):
        r_ext = (img_ct.split("/")[-1].split(";")[0] or "png")[:10]
        blob = f"resize_jobs/{job_id}/result_{i}.{r_ext}"
        try:
            url = upload_bytes_to_gcs(img_bytes, blob, img_ct)
            result_urls.append(url)
        except Exception as exc:
            _log.error("GCS upload failed for result %d of resize job %s: %s", i, job_id, exc)

    if result_urls:
        set_resize_job_completed(job_id, result_urls)
        _log.info("Resize job %s completed — %d result(s)", job_id, len(result_urls))
        return {"ok": True, "result_count": len(result_urls)}
    else:
        set_resize_job_failed(job_id, "Failed to store resized images to cloud storage")
        return {"ok": False}


@router.get("")
async def list_resize_jobs_endpoint(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """List the current user's resize job history (most recent first)."""
    _check_access(current_user)
    docs = list_resize_jobs(current_user["email"], limit=min(limit, 100))
    return [_to_response(d) for d in docs]


@router.get("/{job_id}")
async def get_resize_job_endpoint(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a single resize job. Poll every 5 s until status != pending."""
    _check_access(current_user)
    doc = get_resize_job(job_id, current_user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")
    return _to_response(doc)

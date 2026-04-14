"""
Creative Resize – Image Resizing Jobs API

PRIMARY FLOW (sync — no callback dependency on n8n):
  1. POST /resize-jobs     → creates DB record (status=pending), fires background task,
                             returns job_id to client in < 200 ms
  2. Background thread     → uploads original to GCS, then POSTs to n8n and WAITS
                             for the full response (n8n finishes in ~30 s)
  3. n8n response body     → contains resized image(s) — parsed here in the background
                             thread, uploaded to GCS, job marked completed
  4. Frontend polls        → GET /resize-jobs/{id} every 5 s until status != pending

FALLBACK (in case n8n workflow variant pushes results asynchronously):
  POST /resize-jobs/{id}/complete  — n8n can still POST results here

Payload sent TO n8n (multipart/form-data):
  - data         : original image file (field name "data")
  - sizes        : JSON string — [{"name":"1200x628","width":1200,"height":628}, …]
  - max_size_kb  : integer string (e.g. "999000")
  - email        : user email
"""
import base64
import hmac
import io
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile

from config import settings
from gcs_utils import upload_bytes_to_gcs
from user_repository import check_and_increment_service_usage
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


# ── Access check ───────────────────────────────────────────────────────────────

def _check_access(user: dict) -> None:
    if "admin" in (user.get("roles") or []) or user.get("is_admin"):
        return
    services = user.get("allowed_services") or []
    if "creative_resize" not in services:
        raise HTTPException(
            status_code=403,
            detail="Your account does not have access to Creative Resize. Contact an admin.",
        )


def _to_response(doc: dict) -> dict:
    sizes = doc.get("sizes") or []
    # Back-compat: old records stored individual target_width / target_height
    if not sizes and doc.get("target_width") and doc.get("target_height"):
        sizes = [{
            "name": f"{doc['target_width']}x{doc['target_height']}",
            "width": doc["target_width"],
            "height": doc["target_height"],
        }]
    return {
        "job_id":            doc["job_id"],
        "status":            doc["status"],
        "created_at":        doc["created_at"],
        "completed_at":      doc.get("completed_at"),
        "original_filename": doc.get("original_filename"),
        "original_url":      doc.get("original_url"),
        "sizes":             sizes,
        "max_size_kb":       doc.get("max_size_kb", 999000),
        "result_urls":       doc.get("result_urls") or [],
        "result_images":     doc.get("result_images") or [],  # [{url, name}, …]
        "error":             doc.get("error"),
    }


# ── Background worker ──────────────────────────────────────────────────────────
#
# PRIMARY FLOW (no callback needed from n8n):
#   _process() runs in a FastAPI background thread.
#   It blocks waiting for the n8n response (up to N8N_TIMEOUT_SECONDS).
#   When n8n finishes (~30 s), the response body contains the resized images.
#   We parse them here, upload to GCS, and mark the job complete — all in
#   the same call.  No need for n8n to know our callback URL.
#
# FALLBACK: the /resize-jobs/{id}/complete callback endpoint still exists in
#   case a future n8n workflow variant pushes results asynchronously.

N8N_TIMEOUT_SECONDS = 120   # n8n finishes in ~30 s; 120 s is a safe ceiling


def _process(
    job_id: str,
    image_data: bytes,
    content_type: str,
    filename: str,
    user_email: str,
    sizes: List[Dict[str, Any]],
    max_size_kb: int,
) -> None:
    """Upload original to GCS, POST to n8n, parse the sync response, store results."""

    # 1. Upload original to GCS (best-effort — non-fatal)
    ext = (content_type.split("/")[-1].split(";")[0] or "jpg")[:10]
    try:
        original_url = upload_bytes_to_gcs(
            image_data, f"resize_jobs/{job_id}/original.{ext}", content_type
        )
        update_resize_original_url(job_id, original_url)
    except Exception as exc:
        _log.warning("Original GCS upload failed for resize job %s (non-fatal): %s", job_id, exc)

    # 2. Guard: webhook must be configured
    if not settings.N8N_RESIZE_WEBHOOK_URL:
        _log.error("N8N_RESIZE_WEBHOOK_URL not configured; resize job %s failed", job_id)
        set_resize_job_failed(job_id, "Image resize service not configured on server.")
        return

    # 3. POST to n8n and wait for the full response
    try:
        import requests as _req

        _log.info("POSTing resize job %s to n8n (%d size(s)) — waiting up to %ds…",
                  job_id, len(sizes), N8N_TIMEOUT_SECONDS)

        resp = _req.post(
            settings.N8N_RESIZE_WEBHOOK_URL,
            files={"data": (filename, io.BytesIO(image_data), content_type)},
            data={
                "sizes":       json.dumps(sizes),
                "max_size_kb": str(max_size_kb),
                "email":       user_email,
            },
            timeout=N8N_TIMEOUT_SECONDS,
        )

        if not resp.ok:
            _log.error("n8n HTTP %s for resize job %s: %s",
                       resp.status_code, job_id, resp.text[:300])
            set_resize_job_failed(job_id, f"Processing service returned HTTP {resp.status_code}")
            return

        _log.info("n8n responded HTTP %s for resize job %s (%.1f s)",
                  resp.status_code, job_id, resp.elapsed.total_seconds())

        # ── DEBUG: log full raw response so we know exactly what n8n returns ──
        _log.info("n8n response Content-Type: %s", resp.headers.get("content-type"))
        try:
            _log.info("n8n response body (JSON): %s", json.dumps(resp.json(), indent=2)[:2000])
        except Exception:
            _log.info("n8n response body (raw bytes[:500]): %s", resp.content[:500])

    except Exception as exc:
        _log.error("Failed to reach n8n for resize job %s: %s", job_id, exc)
        set_resize_job_failed(job_id, str(exc))
        return

    # 4. Parse the n8n response and extract image(s)
    result_pairs = _extract_images_from_response(resp)

    if not result_pairs:
        _log.error(
            "Resize job %s: could not extract images from n8n response.\n"
            "  Content-Type : %s\n"
            "  Body (first 1000 bytes): %s",
            job_id,
            resp.headers.get("content-type"),
            resp.content[:1000],
        )
        set_resize_job_failed(job_id, "n8n response contained no image data.")
        return

    # 5. Upload each resized image to GCS; collect {url, name} metadata
    result_images = _upload_result_pairs(job_id, result_pairs)

    if result_images:
        result_urls = [ri["url"] for ri in result_images]
        set_resize_job_completed(job_id, result_urls, result_images)
        _log.info("Resize job %s completed — %d result(s) stored in GCS", job_id, len(result_urls))
    else:
        set_resize_job_failed(job_id, "Failed to store resized images to cloud storage.")


def _extract_images_from_response(resp: Any) -> List[tuple]:
    """
    Extract (bytes_or_url, content_type, name) triples from the raw n8n HTTP response.

    Handles four response shapes:

    1. Binary image  — Content-Type: image/png (single image in body)
    2. JSON array    — the actual Ad-lens-final-prod format:
         [{"email":"…","emailBody":"…","attachmentKeys":"img1,img2",
           "images":[{"name":"img1","data":"<base64>"},{"name":"img2","data":"<base64>"}]}]
       Also handles legacy shapes:
         [{"binary": {"data": {"data": [...], "mimeType": "image/png"}}}, …]
         [{"name":"1200x628", "url":"https://…"}, …]
    3. JSON dict     — {"images": [{data, content_type}, …]} or {"image": "…"}
    4. Raw binary fallback
    """
    result_pairs: List[tuple] = []   # (bytes_or_url, content_type, name)
    ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()

    # ── Shape 1: raw binary image ────────────────────────────────────────────
    if ct.startswith("image/"):
        result_pairs.append((resp.content, ct, ""))
        return result_pairs

    # ── Shape 2 & 3: JSON ────────────────────────────────────────────────────
    if "json" in ct:
        try:
            body = resp.json()
        except Exception:
            return result_pairs

        # JSON array — iterate items
        if isinstance(body, list):
            for item in body:
                if not isinstance(item, dict):
                    continue

                # ── Priority A: n8n binary envelope ──────────────────────────
                binary = item.get("binary")
                if isinstance(binary, dict):
                    for _key, bval in binary.items():
                        mime = bval.get("mimeType", "image/png")
                        raw = bval.get("data")
                        if raw:
                            try:
                                result_pairs.append((base64.b64decode(raw), mime, _key))
                                continue
                            except Exception:
                                pass
                        file_path = bval.get("filePathShort") or bval.get("filePath")
                        if file_path and file_path.startswith("http"):
                            result_pairs.append((file_path, mime, _key))
                    continue

                # ── Priority B: nested images array ──────────────────────────
                # Actual Ad-lens-final-prod format:
                # {email, emailBody, attachmentKeys, images:[{name, data}, …]}
                nested_images = item.get("images")
                if isinstance(nested_images, list) and nested_images:
                    for img_item in nested_images:
                        if not isinstance(img_item, dict):
                            continue
                        raw_b64 = img_item.get("data", "")
                        img_name = img_item.get("name", "")
                        if not raw_b64:
                            continue
                        cleaned = (str(raw_b64).split(",", 1)[-1]
                                   if "," in str(raw_b64) else str(raw_b64))
                        try:
                            result_pairs.append((
                                base64.b64decode(cleaned),
                                "image/jpeg",
                                img_name,
                            ))
                        except Exception as exc:
                            _log.warning("Could not decode base64 image '%s': %s", img_name, exc)
                    continue   # move on to the next list item

                # ── Priority C: flat URL or base64 at item level ──────────────
                url = item.get("url") or item.get("image_url")
                if url and isinstance(url, str) and url.startswith("http"):
                    result_pairs.append((
                        url,
                        item.get("content_type", "image/png"),
                        item.get("name", ""),
                    ))
                    continue
                raw = item.get("data") or item.get("image")
                if raw:
                    cleaned = str(raw).split(",", 1)[-1] if "," in str(raw) else str(raw)
                    try:
                        result_pairs.append((
                            base64.b64decode(cleaned),
                            item.get("content_type", "image/png"),
                            item.get("name", ""),
                        ))
                    except Exception:
                        pass
            return result_pairs

        # JSON dict shapes
        if isinstance(body, dict):
            # {"images": [{data, content_type, name?}, …]}
            images_list = body.get("images")
            if isinstance(images_list, list):
                for item in images_list:
                    if not isinstance(item, dict):
                        continue
                    raw_b64 = item.get("data", "")
                    item_ct = item.get("content_type", "image/jpeg")
                    item_name = item.get("name", "")
                    try:
                        result_pairs.append((base64.b64decode(raw_b64), item_ct, item_name))
                    except Exception:
                        url = item.get("url") or item.get("image_url")
                        if url and isinstance(url, str) and url.startswith("http"):
                            result_pairs.append((url, item_ct, item_name))
                return result_pairs

            # {"image": "<b64 or url>", "content_type": "…"}
            raw = (body.get("image") or body.get("data")
                   or body.get("result") or body.get("output"))
            img_ct = body.get("content_type", "image/png")
            if isinstance(raw, str) and raw.startswith("http"):
                result_pairs.append((raw, img_ct, ""))
            elif isinstance(raw, str):
                cleaned = raw.split(",", 1)[-1] if "," in raw else raw
                try:
                    result_pairs.append((base64.b64decode(cleaned), img_ct, ""))
                except Exception:
                    pass

        return result_pairs

    # ── Shape 4: treat anything else as raw binary (best-effort) ────────────
    if resp.content:
        result_pairs.append((resp.content, "image/png", ""))

    return result_pairs


def _upload_result_pairs(job_id: str, pairs: List[tuple]) -> List[Dict[str, Any]]:
    """
    Upload result images to GCS and return a list of {url, name} dicts.

    Each pair is (bytes_or_url, content_type, name).

    If an item is already a URL string:
      - Try to download it and re-upload to GCS (so we own the file).
      - If GCS upload fails, fall back to storing the URL directly.

    If an item is raw bytes:
      - Upload to GCS; log error on failure (no fallback for raw bytes).
    """
    import requests as _req
    result_images: List[Dict[str, Any]] = []

    for i, triple in enumerate(pairs):
        img, img_ct, img_name = triple if len(triple) == 3 else (*triple, "")
        original_url = img if (isinstance(img, str) and img.startswith("http")) else None

        if original_url:
            try:
                dl = _req.get(original_url, timeout=60)
                img_bytes = dl.content
                img_ct = dl.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            except Exception as exc:
                _log.warning("Could not download n8n result URL for job %s[%d]: %s — storing URL directly",
                             job_id, i, exc)
                result_images.append({"url": original_url, "name": img_name or f"result_{i + 1}"})
                continue
        else:
            img_bytes = img

        r_ext = (img_ct.split("/")[-1].split(";")[0] or "jpg")[:10]
        # Use the image name (e.g. "img1") in the GCS path for traceability
        safe_name = (img_name or f"result_{i}").replace(" ", "_")[:40]
        blob = f"resize_jobs/{job_id}/{safe_name}.{r_ext}"
        try:
            gcs_url = upload_bytes_to_gcs(img_bytes, blob, img_ct)
            result_images.append({"url": gcs_url, "name": img_name or f"result_{i + 1}"})
        except Exception as exc:
            _log.warning("GCS upload failed for job %s[%d]: %s", job_id, i, exc)
            if original_url:
                _log.info("Falling back to n8n URL for job %s[%d]: %s", job_id, i, original_url)
                result_images.append({"url": original_url, "name": img_name or f"result_{i + 1}"})
            else:
                _log.error("GCS upload failed and no fallback URL for job %s[%d] — result lost", job_id, i)

    return result_images


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("")
async def create_resize_job(
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    sizes: str = Form(...),            # JSON string: [{"name":"…","width":W,"height":H}, …]
    max_size_kb: int = Form(999000),
    email: Optional[str] = Form(None), # notification email; defaults to authenticated user's email
    current_user: dict = Depends(get_current_user),
):
    """
    Submit an image for resizing to one or more target formats.

    sizes: JSON array of size objects, e.g.:
      [{"name":"1200x628","width":1200,"height":628},
       {"name":"1200x1200","width":1200,"height":1200}]

    max_size_kb: maximum output file size in KB (default 999000 = ~1 GB, effectively unlimited)
    email: notification / delivery email (defaults to authenticated user's email if omitted)
    """
    _check_access(current_user)

    if not check_and_increment_service_usage(current_user, "creative_resize"):
        raise HTTPException(
            status_code=429,
            detail="Monthly usage limit reached for Creative Resize. Contact an admin to increase your limit.",
        )

    # Parse and validate sizes
    try:
        sizes_list: List[Dict] = json.loads(sizes)
        if not isinstance(sizes_list, list) or len(sizes_list) == 0:
            raise ValueError("sizes must be a non-empty array")
        for s in sizes_list:
            if not isinstance(s.get("width"), int) or not isinstance(s.get("height"), int):
                raise ValueError(f"Each size must have integer width and height: {s}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid sizes parameter: {exc}")

    # Resolve notification email — user-supplied value or fall back to authenticated email
    notify_email = (email or "").strip() or current_user["email"]

    image_data = await image.read()
    if len(image_data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 20 MB)")

    content_type = (image.content_type or "image/jpeg").split(";")[0].strip()
    safe_filename = image.filename or "image.jpg"

    job_id = create_resize_job_record(
        user_email=current_user["email"],
        original_filename=safe_filename,
        sizes=sizes_list,
        max_size_kb=max_size_kb,
    )

    background_tasks.add_task(
        _process,
        job_id,
        image_data,
        content_type,
        safe_filename,
        notify_email,
        sizes_list,
        max_size_kb,
    )

    doc = get_resize_job(job_id, current_user["email"])
    return _to_response(doc)


@router.post("/{job_id}/complete")
async def resize_job_complete(job_id: str, request: Request):
    """
    Fallback callback endpoint in case an n8n workflow variant pushes results
    asynchronously.  The primary path is sync — _process() waits for the n8n
    response directly.  This endpoint is only hit if n8n is configured to POST
    results back here.

    Expected body (JSON):
      { "callback_secret": "…", "images": [{data, content_type}] }
    """
    body = await request.json()

    received_secret = body.get("callback_secret", "")
    if not hmac.compare_digest(received_secret, settings.N8N_CALLBACK_SECRET):
        _log.warning("Invalid callback_secret for resize job %s", job_id)
        raise HTTPException(status_code=403, detail="Invalid callback secret")

    # Wrap in a mock Response-like object so _extract_images_from_response can handle it
    class _FakeResp:
        content = b""
        headers = {"content-type": "application/json"}
        def json(self_inner):
            return body

    result_pairs = _extract_images_from_response(_FakeResp())

    if not result_pairs:
        _log.error("Callback for resize job %s had no image data", job_id)
        set_resize_job_failed(job_id, "Callback contained no image data")
        return {"ok": False}

    result_images = _upload_result_pairs(job_id, result_pairs)

    if result_images:
        result_urls = [ri["url"] for ri in result_images]
        set_resize_job_completed(job_id, result_urls, result_images)
        _log.info("Resize job %s completed via callback — %d result(s)", job_id, len(result_urls))
        return {"ok": True, "result_count": len(result_urls)}
    else:
        set_resize_job_failed(job_id, "Failed to store resized images to cloud storage")
        return {"ok": False}


@router.get("/{job_id}/results/{image_index}/download")
async def download_resize_result(
    job_id: str,
    image_index: int,
    current_user: dict = Depends(get_current_user),
):
    """
    Proxy-download a single result image from GCS with Content-Disposition: attachment
    so the browser saves the file instead of opening it in a new tab.
    """
    import requests as _req
    from fastapi.responses import StreamingResponse

    doc = get_resize_job(job_id, current_user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")

    result_images = doc.get("result_images") or []
    result_urls   = doc.get("result_urls") or []

    if result_images and image_index < len(result_images):
        gcs_url  = result_images[image_index]["url"]
        img_name = result_images[image_index].get("name", f"result_{image_index + 1}")
    elif image_index < len(result_urls):
        gcs_url  = result_urls[image_index]
        img_name = f"result_{image_index + 1}"
    else:
        raise HTTPException(404, "Image index out of range")

    try:
        r = _req.get(gcs_url, timeout=60, stream=True)
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch image from storage: {exc}")

    ct  = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    ext = ct.split("/")[-1] or "jpg"
    safe_name = img_name.replace(" ", "_")[:60]
    filename  = f"{safe_name}.{ext}"

    def _stream():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type=ct,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

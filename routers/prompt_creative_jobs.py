"""
Prompt Creative – Brand RAG Text-to-Image Jobs API

Wraps the n8n "Brand RAG Image Generation" workflow (webhook path: generate-image):
brand + logo guidelines are retrieved from Pinecone, Gemini plans the creative, and
Gemini image generation returns two finished variants (v1, v2).

Async callback pattern (no 504):
  1. POST /prompt-creative-jobs              → DB record, fire background task, return job_id in < 200ms
  2. Background task                         → POST prompt to n8n with callback_url (Header Auth)
  3. n8n workflow                            → acks immediately, generates, POSTs results back
     (if the workflow is instead set to answer synchronously with the images, they are stored directly)
  4. POST /prompt-creative-jobs/{id}/complete → receives results, uploads to GCS, marks completed
  5. GET  /prompt-creative-jobs/{id}         → poll until status = completed | failed
  6. GET  /prompt-creative-jobs              → list user's history
"""
import base64
import hmac
import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from config import settings
from gcs_utils import upload_bytes_to_gcs
from user_repository import check_usage_with_org
from prompt_creative_job_repository import (
    create_prompt_creative_job_record,
    get_prompt_creative_job,
    get_prompt_creative_job_any,
    list_prompt_creative_jobs,
    set_prompt_creative_job_completed,
    set_prompt_creative_job_failed,
    set_prompt_creative_job_processing,
)
from routers.auth import get_current_user

router = APIRouter(prefix="/prompt-creative-jobs", tags=["prompt-creative-jobs"])
_log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 2000  # mirrors the workflow's "Prompt Provided?" check
# Aspect ratios accepted by Gemini image generation
ASPECT_RATIOS = {"1:1", "4:5", "3:4", "2:3", "3:2", "4:3", "9:16", "16:9", "21:9"}
DEFAULT_ASPECT_RATIO = "16:9"  # the workflow's original hard-coded value


class PromptCreativeCreate(BaseModel):
    prompt: str
    aspect_ratio: Optional[str] = None


# ── Access check ──────────────────────────────────────────────────────────────

def _check_access(user: dict) -> None:
    """Allow access if the user has 'prompt_creative' in allowed_services. Admins always pass."""
    roles = user.get("roles") or []
    if "admin" in roles:
        return
    services = user.get("allowed_services") or ["abcd_analyzer"]
    if "prompt_creative" not in services:
        raise HTTPException(
            status_code=403,
            detail="Your account does not have access to Prompt Creative. Contact an admin.",
        )


def _to_response(doc: dict) -> dict:
    return {
        "job_id":       doc["job_id"],
        "status":       doc["status"],
        "created_at":   doc["created_at"],
        "completed_at": doc.get("completed_at"),
        "prompt":       doc.get("prompt"),
        "aspect_ratio": doc.get("aspect_ratio"),
        "result_urls":  doc.get("result_urls") or [],
        "error":        doc.get("error"),
    }


def _expire_if_stale(doc: dict) -> dict:
    """
    n8n only calls back on success — if the workflow errors, the job would poll forever.
    Fail jobs that have been in flight longer than PROMPT_CREATIVE_TIMEOUT_MINUTES.
    """
    if doc.get("status") not in ("pending", "processing"):
        return doc
    try:
        created = datetime.strptime(doc["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, ValueError):
        return doc
    if datetime.utcnow() - created > timedelta(minutes=settings.PROMPT_CREATIVE_TIMEOUT_MINUTES):
        error = "Generation timed out — the creative service did not return any images. Please try again."
        set_prompt_creative_job_failed(doc["job_id"], error)
        doc = {**doc, "status": "failed", "error": error}
    return doc


# ── Result storage (shared by the sync response path and the callback) ───────

def _decode_images(images: Any) -> List[tuple]:
    """
    Normalise n8n image entries to (bytes, content_type) pairs. Accepts:
      {"data": "<base64>", "content_type": "image/png"}   (callback shape)
      {"dataUrl": "data:image/png;base64,...", "mimeType": "image/png"}  (Package Webhook Response shape)
    """
    pairs: List[tuple] = []
    if not isinstance(images, list):
        return pairs
    for item in images:
        if not isinstance(item, dict):
            continue
        raw = item.get("data") or item.get("dataUrl") or item.get("imageBase64")
        if not raw or not isinstance(raw, str):
            continue
        ct = item.get("content_type") or item.get("mimeType") or item.get("imageMimeType") or "image/png"
        if raw.startswith("data:"):
            header, _, raw = raw.partition(",")
            ct = header[5:].split(";")[0] or ct
        try:
            pairs.append((base64.b64decode(raw), ct))
        except Exception as exc:
            _log.warning("Could not decode a prompt creative image entry: %s", exc)
    return pairs


def _store_results(job_id: str, images: Any) -> bool:
    """Upload decoded images to GCS and mark the job completed. Returns True on success."""
    doc = get_prompt_creative_job_any(job_id)
    if not doc:
        _log.warning("Results received for unknown prompt creative job %s", job_id)
        return False
    if doc.get("status") == "completed":
        return True  # already stored (sync response and callback can both arrive)

    pairs = _decode_images(images)
    if not pairs:
        set_prompt_creative_job_failed(job_id, "Generation completed but returned no valid images.")
        return False

    result_urls: List[str] = []
    for i, (img_bytes, img_ct) in enumerate(pairs):
        ext = (img_ct.split("/")[-1].split(";")[0] or "png")[:10]
        try:
            result_urls.append(
                upload_bytes_to_gcs(img_bytes, f"prompt_creative_jobs/{job_id}/result_{i}.{ext}", img_ct)
            )
        except Exception as exc:
            _log.error("GCS upload failed for result %d of prompt creative job %s: %s", i, job_id, exc)

    if not result_urls:
        set_prompt_creative_job_failed(job_id, "Failed to store result images to cloud storage")
        return False

    set_prompt_creative_job_completed(job_id, result_urls)
    _log.info("Prompt creative job %s completed — %d result(s)", job_id, len(result_urls))
    return True


# ── Background worker ─────────────────────────────────────────────────────────

def _process(job_id: str, prompt: str, aspect_ratio: str, callback_url: str) -> None:
    """
    Runs in FastAPI's thread pool — response has already been sent.
    POSTs the prompt to n8n. The webhook normally acks immediately and calls back later;
    if it instead answers with the images, they are stored straight away.
    """
    if not settings.N8N_PROMPT_CREATIVE_WEBHOOK_URL:
        _log.warning("N8N_PROMPT_CREATIVE_WEBHOOK_URL not configured; job %s failed", job_id)
        set_prompt_creative_job_failed(job_id, "Creative generation service not configured on server.")
        return

    try:
        import requests as _req
        from requests.exceptions import ReadTimeout, ConnectionError as ReqConnError

        headers = {"Content-Type": "application/json"}
        if settings.N8N_PROMPT_CREATIVE_AUTH_HEADER:
            headers[settings.N8N_PROMPT_CREATIVE_AUTH_HEADER] = settings.N8N_PROMPT_CREATIVE_AUTH_VALUE

        payload = {
            "prompt":          prompt,
            "aspect_ratio":    aspect_ratio,
            "job_id":          job_id,
            "callback_url":    callback_url,
            "callback_secret": settings.N8N_CALLBACK_SECRET,
        }

        _log.info("Sending prompt creative job %s to n8n…", job_id)
        try:
            resp = _req.post(
                settings.N8N_PROMPT_CREATIVE_WEBHOOK_URL,
                json=payload,
                headers=headers,
                timeout=300,  # long enough for a synchronous run (RAG + planner + 2 image generations)
            )
        except ReadTimeout:
            # n8n received the request and is still working — wait for the callback.
            _log.warning("n8n read timeout for prompt creative job %s — waiting for callback", job_id)
            set_prompt_creative_job_processing(job_id)
            return
        except ReqConnError as exc:
            _log.error("Cannot connect to n8n for prompt creative job %s: %s", job_id, exc)
            set_prompt_creative_job_failed(job_id, "Creative generation service is unreachable. Try again later.")
            return

        if resp.status_code in (502, 503, 504, 524):
            # A proxy in front of n8n gave up waiting for a synchronous answer — the run
            # continues in n8n and the callback will still deliver the images.
            _log.warning("Gateway timeout (HTTP %s) for prompt creative job %s — waiting for callback",
                         resp.status_code, job_id)
            set_prompt_creative_job_processing(job_id)
            return
        if resp.status_code in (401, 403):
            _log.error("n8n rejected auth for prompt creative job %s (HTTP %s)", job_id, resp.status_code)
            set_prompt_creative_job_failed(job_id, "Creative generation service rejected the request (auth).")
            return
        if not resp.ok:
            _log.error("n8n returned HTTP %s for prompt creative job %s: %s",
                       resp.status_code, job_id, resp.text[:200])
            set_prompt_creative_job_failed(job_id, f"Creative generation service returned HTTP {resp.status_code}")
            return

        # Synchronous mode: the workflow answered with the images themselves
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("images"):
            _store_results(job_id, body["images"])
            return

        _log.info("n8n ack for prompt creative job %s (HTTP %s) — marking processing", job_id, resp.status_code)
        set_prompt_creative_job_processing(job_id)

    except Exception as exc:
        _log.error("Unexpected error in prompt creative background task for job %s: %s", job_id, exc)
        set_prompt_creative_job_failed(job_id, str(exc))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
async def create_prompt_creative_job(
    body: PromptCreativeCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """
    Submit a creative brief. Returns in < 200ms.
    Poll GET /prompt-creative-jobs/{id} every 5 s until status = completed | failed.
    """
    _check_access(current_user)

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"Prompt is too long (max {MAX_PROMPT_CHARS} characters)")
    aspect_ratio = body.aspect_ratio or DEFAULT_ASPECT_RATIO
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(400, f"aspect_ratio must be one of {sorted(ASPECT_RATIOS)}")

    allowed, reason = check_usage_with_org(current_user, "prompt_creative")
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    job_id = create_prompt_creative_job_record(
        user_email=current_user["email"],
        prompt=prompt,
        aspect_ratio=aspect_ratio,
    )

    # Prefer BACKEND_PUBLIC_URL (required for local dev since n8n cannot reach localhost).
    if settings.BACKEND_PUBLIC_URL:
        base = settings.BACKEND_PUBLIC_URL.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    callback_url = f"{base}/prompt-creative-jobs/{job_id}/complete"

    background_tasks.add_task(_process, job_id, prompt, aspect_ratio, callback_url)

    doc = get_prompt_creative_job(job_id, current_user["email"])
    return _to_response(doc)


@router.post("/{job_id}/complete")
async def prompt_creative_job_complete(job_id: str, request: Request):
    """
    Callback endpoint called by n8n after generation is done.

    Expected JSON body:
    {
      "callback_secret": "<secret>",
      "images": [{"data": "<base64>", "content_type": "image/png"}, ...]
    }
    """
    body = await request.json()

    received_secret = str(body.get("callback_secret", ""))
    if not hmac.compare_digest(received_secret, settings.N8N_CALLBACK_SECRET):
        _log.warning("Invalid callback_secret for prompt creative job %s", job_id)
        raise HTTPException(status_code=403, detail="Invalid callback secret")

    if body.get("status") == "error":
        set_prompt_creative_job_failed(job_id, str(body.get("message") or "Creative generation failed."))
        return {"ok": False}

    import asyncio
    ok = await asyncio.get_event_loop().run_in_executor(None, _store_results, job_id, body.get("images"))
    return {"ok": ok}


@router.get("")
async def list_prompt_creative_jobs_endpoint(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """List the current user's prompt creative history (most recent first)."""
    _check_access(current_user)
    docs = list_prompt_creative_jobs(current_user["email"], limit=min(limit, 100))
    return [_to_response(_expire_if_stale(d)) for d in docs]


@router.get("/{job_id}/results/{image_index}/download")
async def download_prompt_creative_result(
    job_id: str,
    image_index: int,
    current_user: dict = Depends(get_current_user),
):
    """Proxy-download a result image from GCS so the browser saves it instead of opening it."""
    import requests as _req
    from fastapi.responses import StreamingResponse

    _check_access(current_user)
    doc = get_prompt_creative_job(job_id, current_user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")

    result_urls = doc.get("result_urls") or []
    if image_index < 0 or image_index >= len(result_urls):
        raise HTTPException(404, "Image index out of range")

    try:
        r = _req.get(result_urls[image_index], timeout=60, stream=True)
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch image from storage: {exc}")

    ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
    ext = ct.split("/")[-1] or "png"
    filename = f"prompt_creative_{job_id[:8]}_v{image_index + 1}.{ext}"

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
async def get_prompt_creative_job_endpoint(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a single prompt creative job. Poll every 5 s until status is completed | failed."""
    _check_access(current_user)
    doc = get_prompt_creative_job(job_id, current_user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")
    return _to_response(_expire_if_stale(doc))

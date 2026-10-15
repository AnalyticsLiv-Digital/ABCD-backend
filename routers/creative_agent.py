"""
Creative Agent – one image → up to CREATIVE_AGENT_MAX_TARGETS aspect ratios / sizes.

Each requested output is its own job and its own n8n run ("Adlens - Creative Agent"
workflow), dispatched in parallel. One input → one output per run.

Flow:
  1. POST /creative-agent/batches            → validate, take N runs (all-or-nothing), create
                                                batch + N jobs, return immediately
  2. Background task                          → store original in GCS once, then POST every job
                                                to n8n in parallel; n8n acks with {"status":"received"}
  3. n8n                                      → POSTs the result to callback_url when done
  4. POST /creative-agent/jobs/{id}/complete  → verify per-job token, store result in GCS
  5. GET  /creative-agent/batches/{id}        → single poll: batch + every job's status
  6. POST /creative-agent/jobs/{id}/retry     → re-run one failed output from the stored original
  7. GET  /creative-agent/batches/{id}/download, /jobs/{id}/download → ZIP / single file

Payload sent TO n8n (multipart/form-data, one call per job):
  image, prompt ("I want to change this image to <ratio> aspect ratio (<name>)[. <instructions>]"
  — same format as Creative Studio), sizes (JSON, exactly one entry), max_size_kb (user-chosen, KB),
  job_id, batch_id, callback_url, callback_secret (per-job token)

Callback FROM n8n (JSON):
  success: {"callback_secret": "...", "images": [{"data": "<base64>", "content_type": "..."}]}
  failure: {"callback_secret": "...", "status": "failed", "error": "..."}
"""
import base64
import hmac
import io
import json
import logging
import re
import secrets
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from math import gcd
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from config import settings
from creative_agent_repository import (
    COMPLETED,
    FAILED,
    IN_FLIGHT,
    create_batch,
    get_batch,
    get_job,
    get_job_for_callback,
    hash_token,
    list_batch_jobs,
    list_batches,
    reset_job_for_retry,
    restore_failed,
    set_batch_original_url,
    set_job_completed,
    set_job_failed,
    set_job_processing,
)
from gcs_utils import upload_bytes_to_gcs
from routers.auth import get_current_user
from user_repository import check_usage_with_org

router = APIRouter(prefix="/creative-agent", tags=["creative-agent"])
_log = logging.getLogger(__name__)

SERVICE_ID = "creative_agent"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_DIM, MAX_DIM = 50, 8000
MIN_MAX_SIZE_KB, MAX_MAX_SIZE_KB = 10, 999000   # bounds for the user-chosen max output size
N8N_ACK_TIMEOUT_SECONDS = 60   # the workflow responds right after its image check


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_access(user: dict) -> None:
    if "admin" in (user.get("roles") or []) or user.get("is_admin"):
        return
    if SERVICE_ID not in (user.get("allowed_services") or []):
        raise HTTPException(
            status_code=403,
            detail="Your account does not have access to Creative Resize Agent. Contact an admin.",
        )


def _ratio_label(w: int, h: int) -> str:
    """1200x2400 → '1:2'; 1200x628 → '1.91:1' (decimal when the reduced ratio is unwieldy)."""
    g = gcd(w, h)
    a, b = w // g, h // g
    if a <= 32 and b <= 32:
        return f"{a}:{b}"
    if w >= h:
        return f"{w / h:.2f}".rstrip("0").rstrip(".") + ":1"
    return "1:" + f"{h / w:.2f}".rstrip("0").rstrip(".")


def _build_prompt(target: Dict[str, Any], instructions: Optional[str]) -> str:
    """Same format as Creative Studio: '<ratio sentence>. <user prompt>'. Pixel size travels in `sizes`."""
    prompt = f"I want to change this image to {target['ratio']} aspect ratio ({target['label']})"
    return f"{prompt}. {instructions}" if instructions else prompt


_RATIO_RE = re.compile(r"^\d{1,3}(\.\d{1,2})?:\d{1,3}(\.\d{1,2})?$")


def _client_ratio(raw: Any, w: int, h: int) -> Optional[str]:
    """Accept the picker's canonical ratio (e.g. '8:1' for 728x90) when it matches the size within 3%."""
    if not isinstance(raw, str) or not _RATIO_RE.match(raw.strip()):
        return None
    a, b = (float(x) for x in raw.strip().split(":"))
    if a <= 0 or b <= 0 or abs((a / b) / (w / h) - 1) > 0.03:
        return None
    return raw.strip()


def _parse_targets(raw: str) -> List[Dict[str, Any]]:
    """Validate the client's targets JSON → [{kind, label, width, height, ratio}, …]."""
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid targets.")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Select at least one aspect ratio or size.")
    max_targets = settings.CREATIVE_AGENT_MAX_TARGETS
    if len(items) > max_targets:
        raise HTTPException(400, f"You can request up to {max_targets} outputs per image.")

    targets, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(400, "Invalid targets.")
        w, h = item.get("width"), item.get("height")
        if not isinstance(w, int) or not isinstance(h, int) or isinstance(w, bool) or isinstance(h, bool):
            raise HTTPException(400, "Each output needs a whole-number width and height.")
        if not (MIN_DIM <= w <= MAX_DIM and MIN_DIM <= h <= MAX_DIM):
            raise HTTPException(400, f"Width and height must be between {MIN_DIM} and {MAX_DIM} px.")
        if (w, h) in seen:
            raise HTTPException(400, f"{w}×{h} was selected twice.")
        seen.add((w, h))
        kind = "ratio" if item.get("kind") == "ratio" else "size"
        ratio = _client_ratio(item.get("ratio"), w, h) or _ratio_label(w, h)
        label = str(item.get("label") or "").strip()[:60] or "Custom"
        targets.append({"kind": kind, "label": label, "width": w, "height": h, "ratio": ratio})
    return targets


def _callback_url(request: Request, job_id: str) -> str:
    """
    BACKEND_PUBLIC_URL is required locally (n8n can't reach localhost). Without it, behind
    Cloud Run's TLS proxy request.base_url is http:// — an http POST gets redirected and
    dropped, so honour X-Forwarded-Proto.
    """
    if settings.BACKEND_PUBLIC_URL:
        base = settings.BACKEND_PUBLIC_URL
    else:
        base = str(request.base_url)
        if request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https" and base.startswith("http://"):
            base = "https://" + base[len("http://"):]
    return f"{base.rstrip('/')}/creative-agent/jobs/{job_id}/complete"


def _sniff_content_type(data: bytes, declared: str) -> str:
    """The workflow labels its JPEG output with Gemini's mime type — trust the bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return declared if declared.startswith("image/") else "image/jpeg"


def _expire_if_stale(job: dict) -> dict:
    """Fail outputs in flight longer than CREATIVE_AGENT_TIMEOUT_MINUTES (n8n crashed silently)."""
    if job.get("status") not in IN_FLIGHT:
        return job
    try:
        started = datetime.strptime(job.get("dispatched_at") or job["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return job
    if datetime.utcnow() - started <= timedelta(minutes=settings.CREATIVE_AGENT_TIMEOUT_MINUTES):
        return job
    error = "Timed out — the agent did not return a result. Please retry."
    if set_job_failed(job["job_id"], error):
        job = {**job, "status": FAILED, "error": error}
    return job


def _job_response(job: dict) -> dict:
    t = job.get("target") or {}
    return {
        "job_id":       job["job_id"],
        "index":        job.get("index", 0),
        "kind":         t.get("kind"),
        "label":        t.get("label"),
        "width":        t.get("width"),
        "height":       t.get("height"),
        "ratio":        t.get("ratio"),
        "status":       job["status"],
        "result_url":   job.get("result_url"),
        "error":        job.get("error"),
        "created_at":   job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "retry_count":  job.get("retry_count", 0),
    }


def _batch_response(batch: dict, jobs: List[dict]) -> dict:
    jobs = [_expire_if_stale(j) for j in jobs]
    completed = sum(1 for j in jobs if j["status"] == COMPLETED)
    failed = sum(1 for j in jobs if j["status"] == FAILED)
    done = completed + failed == len(jobs)
    return {
        "batch_id":          batch["batch_id"],
        "job_id":            batch["batch_id"],  # alias: shared history/welcome cards key on job_id
        "created_at":        batch["created_at"],
        "original_filename": batch.get("original_filename"),
        "original_url":      batch.get("original_url"),
        "source_width":      batch.get("source_width"),
        "source_height":     batch.get("source_height"),
        "instructions":      batch.get("instructions"),
        "max_size_kb":       batch.get("max_size_kb"),
        "total":             len(jobs),
        "completed":         completed,
        "failed":            failed,
        "done":              done,
        "status":            ("completed" if completed else "failed") if done else "processing",
        "result_urls":       [j["result_url"] for j in jobs if j["status"] == COMPLETED and j.get("result_url")],
        "jobs":              [_job_response(j) for j in jobs],
    }


def _load_batch(batch_id: str, email: str) -> tuple:
    batch = get_batch(batch_id, email)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch, list_batch_jobs([batch_id], email)


def _safe_base(filename: Optional[str]) -> str:
    base = (filename or "image").rsplit(".", 1)[0]
    return re.sub(r'[\\/:*?"<>|\s]+', "_", base).strip("_") or "image"


# ── Background work ───────────────────────────────────────────────────────────

def _dispatch(
    job_id: str,
    batch_id: str,
    image_data: bytes,
    content_type: str,
    filename: str,
    prompt: str,
    target: Dict[str, Any],
    callback_url: str,
    token: str,
    max_size_kb: int,
) -> None:
    """POST one output to n8n. n8n acks immediately and calls back with the result."""
    import requests as _req
    from requests.exceptions import ConnectionError as ReqConnError, ReadTimeout

    try:
        resp = _req.post(
            settings.N8N_CREATIVE_AGENT_WEBHOOK_URL,
            files={"image": (filename, io.BytesIO(image_data), content_type)},
            data={
                "prompt":          prompt,
                "sizes":           json.dumps([{"name": f"{target['width']}x{target['height']}",
                                                "width": target["width"], "height": target["height"]}]),
                "max_size_kb":     str(max_size_kb),
                "job_id":          job_id,
                "batch_id":        batch_id,
                "callback_url":    callback_url,
                "callback_secret": token,
            },
            timeout=N8N_ACK_TIMEOUT_SECONDS,
        )
    except ReadTimeout:
        # n8n has the request; the workflow is likely still running and will call back.
        _log.warning("Creative Agent job %s: n8n ack timed out — waiting for callback", job_id)
        set_job_processing(job_id)
        return
    except ReqConnError as exc:
        _log.error("Creative Agent job %s: cannot reach n8n: %s", job_id, exc)
        set_job_failed(job_id, "The creative agent service is unreachable. Try again later.")
        return
    except Exception as exc:
        _log.error("Creative Agent job %s: dispatch error: %s", job_id, exc)
        set_job_failed(job_id, "Could not send the image to the creative agent.")
        return

    if not resp.ok:
        _log.error("Creative Agent job %s: n8n HTTP %s: %s", job_id, resp.status_code, resp.text[:300])
        set_job_failed(job_id, f"Creative agent returned HTTP {resp.status_code}")
        return

    # The workflow answers 200 {"status":"error","message":…} when it found no image.
    try:
        ack = resp.json()
    except ValueError:
        ack = None
    if isinstance(ack, dict) and ack.get("status") == "error":
        set_job_failed(job_id, ack.get("message") or "The creative agent rejected the request.")
        return

    set_job_processing(job_id)


def _process_batch(
    batch_id: str,
    image_data: bytes,
    content_type: str,
    filename: str,
    dispatches: List[Dict[str, Any]],
    max_size_kb: int,
) -> None:
    """Store the original once (needed for retries), then fan out every output in parallel."""
    ext = (content_type.split("/")[-1] or "jpg")[:10]
    try:
        url = upload_bytes_to_gcs(image_data, f"creative_agent/{batch_id}/original.{ext}", content_type)
        set_batch_original_url(batch_id, url)
    except Exception as exc:
        _log.warning("Creative Agent batch %s: original GCS upload failed (non-fatal): %s", batch_id, exc)

    if not settings.N8N_CREATIVE_AGENT_WEBHOOK_URL:
        for d in dispatches:
            set_job_failed(d["job_id"], "Creative agent service is not configured on the server.")
        return

    with ThreadPoolExecutor(max_workers=max(1, len(dispatches))) as pool:
        for d in dispatches:
            pool.submit(
                _dispatch, d["job_id"], batch_id, image_data, content_type, filename,
                d["prompt"], d["target"], d["callback_url"], d["token"], max_size_kb,
            )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/batches")
async def create_agent_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    targets: str = Form(...),          # JSON: [{"kind","width","height","ratio"?: "8:1","label"?: "Leaderboard"}, …]
    instructions: Optional[str] = Form(None),
    source_width: Optional[int] = Form(None),
    source_height: Optional[int] = Form(None),
    max_size_kb: Optional[int] = Form(None),   # max output file size in KB; omitted → server default
    current_user: dict = Depends(get_current_user),
):
    """Upload one image and request up to N outputs. Each output costs one run."""
    _check_access(current_user)
    target_list = _parse_targets(targets)
    instructions = (instructions or "").strip()[:1000] or None
    if max_size_kb is None:
        max_size_kb = settings.CREATIVE_AGENT_MAX_SIZE_KB
    elif not MIN_MAX_SIZE_KB <= max_size_kb <= MAX_MAX_SIZE_KB:
        raise HTTPException(400, f"Max output size must be between {MIN_MAX_SIZE_KB} KB and {MAX_MAX_SIZE_KB} KB.")

    image_data = await image.read()
    content_type = (image.content_type or "image/jpeg").split(";")[0].strip()
    if len(image_data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Image too large (max 20 MB).")
    if not content_type.startswith("image/"):
        raise HTTPException(400, "File is not an image.")

    n = len(target_list)
    allowed, reason = check_usage_with_org(current_user, SERVICE_ID, count=n)
    if not allowed:
        raise HTTPException(429, f"{reason} (this request needs {n} run{'s' if n != 1 else ''}).")

    filename = image.filename or "image.jpg"
    tokens = [secrets.token_urlsafe(32) for _ in target_list]
    batch = create_batch(
        current_user["email"], filename, source_width, source_height, instructions, max_size_kb,
        [
            {"target": t, "prompt": _build_prompt(t, instructions),
             "token_hash": hash_token(tok)}
            for t, tok in zip(target_list, tokens)
        ],
    )
    jobs = list_batch_jobs([batch["batch_id"]], current_user["email"])
    token_by_index = dict(enumerate(tokens))
    dispatches = [
        {"job_id": j["job_id"], "prompt": j["prompt"], "target": j["target"],
         "callback_url": _callback_url(request, j["job_id"]), "token": token_by_index[j["index"]]}
        for j in jobs
    ]
    background_tasks.add_task(_process_batch, batch["batch_id"], image_data, content_type, filename, dispatches, max_size_kb)
    return _batch_response(batch, jobs)


@router.get("/batches")
def list_agent_batches(limit: int = 50, current_user: dict = Depends(get_current_user)):
    """History, most recent first — each batch with its outputs."""
    _check_access(current_user)
    email = current_user["email"]
    batches = list_batches(email, limit=min(max(limit, 1), 100))
    if not batches:
        return []
    jobs_by_batch: Dict[str, List[dict]] = {}
    for j in list_batch_jobs([b["batch_id"] for b in batches], email):
        jobs_by_batch.setdefault(j["batch_id"], []).append(j)
    return [_batch_response(b, jobs_by_batch.get(b["batch_id"], [])) for b in batches]


@router.get("/batches/{batch_id}")
def get_agent_batch(batch_id: str, current_user: dict = Depends(get_current_user)):
    """Batch + every output. Poll every 5 s until done = true."""
    _check_access(current_user)
    return _batch_response(*_load_batch(batch_id, current_user["email"]))


@router.post("/jobs/{job_id}/complete")
async def agent_job_complete(job_id: str, request: Request):
    """
    n8n callback. Authenticated by the per-job token sent as callback_secret.
    Idempotent: a duplicate callback never overwrites a stored result.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid JSON")

    job = get_job_for_callback(job_id)
    received = str(body.get("callback_secret") or "")
    if not job or not hmac.compare_digest(hash_token(received), job.get("callback_token_hash") or ""):
        _log.warning("Creative Agent callback rejected for job %s", job_id)
        raise HTTPException(403, "Invalid callback secret")

    if job["status"] == COMPLETED:
        return {"ok": True, "duplicate": True}

    if str(body.get("status", "")).lower() in ("failed", "error"):
        error = str(body.get("error") or body.get("message") or "The creative agent failed.")[:500]
        set_job_failed(job_id, error)
        return {"ok": True}

    # Accept {"images":[{data, content_type}]} (current workflow) or {"image"|"data": …}
    images = body.get("images")
    entry = images[0] if isinstance(images, list) and images and isinstance(images[0], dict) else body
    raw = entry.get("data") or entry.get("image") or entry.get("url")
    declared = str(entry.get("content_type") or "image/jpeg")

    try:
        if isinstance(raw, str) and raw.startswith("http"):
            import requests as _req
            r = _req.get(raw, timeout=60)
            r.raise_for_status()
            img_bytes = r.content
        elif isinstance(raw, str) and raw.strip():
            img_bytes = base64.b64decode(raw.split(",", 1)[-1] if raw.startswith("data:") else raw)
        else:
            raise ValueError("no image data")
    except Exception as exc:
        _log.error("Creative Agent callback for job %s had no usable image: %s", job_id, exc)
        set_job_failed(job_id, "The creative agent returned no usable image.")
        return {"ok": False}

    ct = _sniff_content_type(img_bytes, declared)
    ext = ct.split("/")[-1]
    try:
        url = upload_bytes_to_gcs(
            img_bytes, f"creative_agent/{job['batch_id']}/{job_id}_r{job.get('retry_count', 0)}.{ext}", ct
        )
    except Exception as exc:
        _log.error("Creative Agent job %s: result GCS upload failed: %s", job_id, exc)
        set_job_failed(job_id, "Could not store the result image.")
        return {"ok": False}

    set_job_completed(job_id, url, ct)
    _log.info("Creative Agent job %s completed (%d bytes)", job_id, len(img_bytes))
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def retry_agent_job(
    job_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """
    Re-run one failed output from the stored original. Costs one run.
    Sync def on purpose: the original is fetched with blocking I/O in the thread pool.
    """
    import requests as _req

    _check_access(current_user)
    email = current_user["email"]
    job = get_job(job_id, email)
    if not job:
        raise HTTPException(404, "Output not found")
    job = _expire_if_stale(job)
    if job["status"] != FAILED:
        raise HTTPException(409, "Only failed outputs can be retried.")
    batch = get_batch(job["batch_id"], email)
    if not batch or not batch.get("original_url"):
        raise HTTPException(409, "The original image was not stored, so it can't be retried. Please upload it again.")

    try:
        r = _req.get(batch["original_url"], timeout=60)
        r.raise_for_status()
    except Exception as exc:
        _log.error("Creative Agent retry: could not fetch original for batch %s: %s", batch["batch_id"], exc)
        raise HTTPException(502, "Could not load the original image from storage. Try again shortly.")
    content_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()

    previous_error = job.get("error") or "Processing failed"
    token = secrets.token_urlsafe(32)
    if not reset_job_for_retry(job_id, email, hash_token(token)):
        raise HTTPException(409, "This output is already being retried.")

    allowed, reason = check_usage_with_org(current_user, SERVICE_ID)
    if not allowed:
        restore_failed(job_id, previous_error)
        raise HTTPException(429, reason)

    background_tasks.add_task(
        _dispatch, job_id, batch["batch_id"], r.content, content_type,
        batch.get("original_filename") or "image.jpg", job["prompt"], job["target"],
        _callback_url(request, job_id), token,
        batch.get("max_size_kb") or settings.CREATIVE_AGENT_MAX_SIZE_KB,
    )
    return _job_response(get_job(job_id, email))


@router.get("/jobs/{job_id}/download")
def download_agent_result(job_id: str, current_user: dict = Depends(get_current_user)):
    """Proxy one result with Content-Disposition: attachment so the browser saves it."""
    import requests as _req

    _check_access(current_user)
    email = current_user["email"]
    job = get_job(job_id, email)
    if not job or job["status"] != COMPLETED or not job.get("result_url"):
        raise HTTPException(404, "Result not available")
    batch = get_batch(job["batch_id"], email) or {}
    try:
        r = _req.get(job["result_url"], timeout=60)
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch image from storage: {exc}")

    ct = job.get("result_content_type") or "image/jpeg"
    t = job["target"]
    name = f"{_safe_base(batch.get('original_filename'))}_{t['width']}x{t['height']}.{ct.split('/')[-1]}"
    return StreamingResponse(
        io.BytesIO(r.content), media_type=ct,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/batches/{batch_id}/download")
def download_agent_batch(batch_id: str, current_user: dict = Depends(get_current_user)):
    """ZIP of every completed output (max 5, so in-memory is fine)."""
    import requests as _req

    _check_access(current_user)
    batch, jobs = _load_batch(batch_id, current_user["email"])
    base = _safe_base(batch.get("original_filename"))

    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job in jobs:
            if job["status"] != COMPLETED or not job.get("result_url"):
                continue
            try:
                r = _req.get(job["result_url"], timeout=60)
                r.raise_for_status()
            except Exception as exc:
                _log.warning("ZIP: could not fetch result of job %s: %s", job["job_id"], exc)
                continue
            t = job["target"]
            ext = (job.get("result_content_type") or "image/jpeg").split("/")[-1]
            zf.writestr(f"{base}_{t['width']}x{t['height']}.{ext}", r.content)
            added += 1

    if not added:
        raise HTTPException(404, "No finished outputs to download yet.")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}_creative_agent.zip"'},
    )

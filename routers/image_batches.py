"""
Creative Studio – multi-image batches (up to IMAGE_BATCH_MAX_FILES images, one prompt).

A batch is a thin parent record. Every image is still a normal image job that goes
through the exact same pipeline as POST /image-jobs (routers/image_jobs._process),
so the n8n workflow and its callback (/image-jobs/{id}/complete) are unchanged.

Flow:
  1. POST /image-batches                               → validate, take N runs of quota
                                                          (all-or-nothing), create batch +
                                                          N pending jobs; returns job_ids
  2. POST /image-batches/{id}/jobs/{job_id}/upload     → one request per file (≤ 20 MB each,
                                                          well under Cloud Run's 32 MB cap);
                                                          dispatches that job to n8n
  3. GET  /image-batches/{id}                          → batch + its jobs (single poll)
  4. GET  /image-batches/{id}/download                 → ZIP of all completed results
"""
import io
import logging
import re
import zipfile
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import settings
from image_job_repository import (
    claim_batch_upload,
    create_image_batch,
    fail_unreceived_upload,
    get_image_batch,
    get_image_job,
    list_image_batch_jobs,
)
from routers.auth import get_current_user
from routers.image_jobs import _callback_url, _check_access, _expire_if_stale, _process, _to_response
from user_repository import check_usage_with_org

router = APIRouter(prefix="/image-batches", tags=["image-batches"])
_log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # same cap as POST /image-jobs


class BatchFileIn(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    size: int = Field(..., ge=0)
    content_type: str = Field("", max_length=100)


class BatchCreateIn(BaseModel):
    prompt: Optional[str] = Field(None, max_length=4000)
    files: List[BatchFileIn]


def _batch_response(batch: dict, jobs: List[dict]) -> dict:
    jobs = [_expire_if_stale(j) for j in jobs]
    completed = sum(1 for j in jobs if j["status"] == "completed")
    failed = sum(1 for j in jobs if j["status"] == "failed")
    return {
        "batch_id":   batch["batch_id"],
        "created_at": batch["created_at"],
        "prompt":     batch.get("prompt"),
        "total":      len(jobs),
        "completed":  completed,
        "failed":     failed,
        "done":       completed + failed == len(jobs),
        "jobs":       [_to_response(j) for j in jobs],
    }


def _load_batch(batch_id: str, user_email: str) -> tuple:
    batch = get_image_batch(batch_id, user_email)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch, list_image_batch_jobs(batch_id, user_email)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
def create_batch(body: BatchCreateIn, current_user: dict = Depends(get_current_user)):
    """
    Create a batch. Validates every file up front and consumes N runs at once, so a
    batch is never half-accepted because quota ran out midway.
    """
    _check_access(current_user)

    n = len(body.files)
    max_files = settings.IMAGE_BATCH_MAX_FILES
    if n < 1:
        raise HTTPException(400, "Select at least one image.")
    if n > max_files:
        raise HTTPException(400, f"You can enhance up to {max_files} images at once.")
    for f in body.files:
        if f.size > MAX_IMAGE_BYTES:
            raise HTTPException(400, f'"{f.filename}" is too large (max 20 MB).')
        if f.content_type and not f.content_type.startswith("image/"):
            raise HTTPException(400, f'"{f.filename}" is not an image.')

    allowed, reason = check_usage_with_org(current_user, "creative_studio", count=n)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"{reason} (this batch needs {n} run{'s' if n != 1 else ''}).")

    prompt = (body.prompt or "").strip() or None
    batch = create_image_batch(current_user["email"], prompt, [f.filename for f in body.files])
    return _batch_response(batch, list_image_batch_jobs(batch["batch_id"], current_user["email"]))


@router.post("/{batch_id}/jobs/{job_id}/upload")
async def upload_batch_image(
    batch_id: str,
    job_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload one file of a batch and dispatch it to n8n (same pipeline as single jobs)."""
    _check_access(current_user)
    email = current_user["email"]

    job = get_image_job(job_id, email)
    if not job or job.get("batch_id") != batch_id:
        raise HTTPException(404, "Image not found in this batch")
    if job.get("upload_received", True) or job.get("status") != "pending":
        detail = "This image slot has expired." if job.get("status") == "failed" else "This image was already uploaded."
        raise HTTPException(409, detail)

    image_data = await image.read()
    content_type = (image.content_type or "image/jpeg").split(";")[0].strip()
    if len(image_data) > MAX_IMAGE_BYTES:
        fail_unreceived_upload(job_id, email, "Image too large (max 20 MB)")
        raise HTTPException(400, "Image too large (max 20 MB)")
    if not content_type.startswith("image/"):
        fail_unreceived_upload(job_id, email, "File is not an image")
        raise HTTPException(400, "File is not an image")

    # Atomic claim — a re-sent upload can never dispatch the same job twice.
    if not claim_batch_upload(job_id, email):
        raise HTTPException(409, "This image was already uploaded.")

    background_tasks.add_task(
        _process,
        job_id,
        image_data,
        content_type,
        job.get("original_filename") or image.filename or "image.jpg",
        job.get("prompt") or "",
        _callback_url(request, job_id),
    )
    return _to_response(get_image_job(job_id, email))


@router.get("/{batch_id}")
def get_batch(batch_id: str, current_user: dict = Depends(get_current_user)):
    """Batch + all its jobs. Poll every 5 s until done = true."""
    _check_access(current_user)
    batch, jobs = _load_batch(batch_id, current_user["email"])
    return _batch_response(batch, jobs)


@router.get("/{batch_id}/download")
def download_batch_zip(batch_id: str, current_user: dict = Depends(get_current_user)):
    """
    ZIP of every completed result in the batch. Sync def on purpose: results are fetched
    from GCS with blocking I/O in the thread pool (max 5 jobs, so in-memory is fine).
    """
    import requests as _req

    _check_access(current_user)
    _, jobs = _load_batch(batch_id, current_user["email"])

    buf = io.BytesIO()
    used_names: set = set()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job in jobs:
            urls = (job.get("result_urls") or []) if job.get("status") == "completed" else []
            base = (job.get("original_filename") or "image").rsplit(".", 1)[0]
            base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip() or "image"
            for i, url in enumerate(urls):
                try:
                    r = _req.get(url, timeout=60)
                    r.raise_for_status()
                except Exception as exc:
                    _log.warning("ZIP: could not fetch result %d of job %s: %s", i, job["job_id"], exc)
                    continue
                ext = r.headers.get("content-type", "image/png").split(";")[0].split("/")[-1] or "png"
                suffix = f"_enhanced_{i + 1}" if len(urls) > 1 else "_enhanced"
                name = f"{base}{suffix}.{ext}"
                n = 2
                while name in used_names:  # two uploads with the same filename
                    name = f"{base}{suffix} ({n}).{ext}"
                    n += 1
                used_names.add(name)
                zf.writestr(name, r.content)
                added += 1

    if not added:
        raise HTTPException(404, "No finished images to download yet.")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="enhanced_images_{batch_id[:8]}.zip"'},
    )

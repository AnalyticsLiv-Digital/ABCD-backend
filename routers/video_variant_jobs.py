"""
Video Variants – shorter cuts and new formats of a video ad, powered by ViGenAiR.

The ViGenAiR engine is a separate Cloud Function (project adlens-vigenair); everything
engine-specific is in services/vigenair/. This router applies the AdLens rules: access,
the 120 s limit, credits (charged before work starts, refunded if it fails) and ownership.

Credits: 1 credit = up to VIDEO_CREDIT_SECONDS (30 s) of video.
  - analysis: once per video, ceil(duration / 30)
  - render:   per output file (cut × format), ceil(cut length / 30)

Flow:
  1. POST /video-variant-jobs/upload-url + browser PUT to GCS + POST /from-upload (UI),
     or POST /video-variant-jobs (multipart, local/small files), or /from-abcd
       → measure, reject > 120 s, charge analysis, copy into the engine bucket (starts analysis)
  2. GET  /video-variant-jobs/{id}                    → poll until status = ready | failed
  3. POST /video-variant-jobs/{id}/suggestions        → Gemini-suggested cuts (free)
  4. POST /video-variant-jobs/{id}/renders:quote      → files + credits, nothing charged
  5. POST /video-variant-jobs/{id}/renders            → charge, write render.json
  6. GET  /video-variant-jobs/{id}/renders/{rid}      → poll until done (files with signed URLs)
  7. POST /video-variant-jobs/{id}/renders/{rid}/files/{n}/score → normal ABCD Analyzer job
"""
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import services.vigenair as vg
from config import settings
from db import jobs_collection
from routers.auth import get_current_user
from user_repository import (
    check_usage_with_org,
    default_service_limit,
    get_user_by_email,
    is_usage_period_stale,
    refund_usage_with_org,
)
from video_variant_job_repository import (
    add_charge,
    create_video_variant_job,
    delete_video_variant_job,
    get_video_variant_job,
    list_video_variant_jobs,
    mark_charge_refunded,
    now_iso,
    push_render,
    rekey_charge,
    set_file_abcd_job,
    set_job_failed,
    update_job,
    update_render,
)

router = APIRouter(prefix="/video-variant-jobs", tags=["video-variant-jobs"])
_log = logging.getLogger(__name__)

SERVICE = "video_variants"
MAX_CUTS_PER_RENDER = 5
MAX_GENERATIONS_PER_VIDEO = 20

# ABCD Analyzer objective → ViGenAiR prompt variant (guide §6.1)
OBJECTIVE_FROM_ABCD = {"awareness": "awareness", "consideration": "consideration", "conversion": "action"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_access(user: dict) -> None:
    if "admin" in (user.get("roles") or []):
        return
    if SERVICE not in (user.get("allowed_services") or []):
        raise HTTPException(403, "Your account does not have access to Video Variants. Contact an admin.")


def _credits_left(email: str) -> Optional[int]:
    """Remaining personal credits this month; None = unlimited (admins)."""
    user = get_user_by_email(email) or {}
    if "admin" in (user.get("roles") or []):
        return None
    limit = int((user.get("service_limits") or {}).get(SERVICE, default_service_limit(SERVICE)))
    used = 0 if is_usage_period_stale(user.get("usage_period_start")) else int(
        (user.get("service_usage") or {}).get(SERVICE, 0)
    )
    return max(0, limit - used)


def _charge(user: dict, job_id: str, kind: str, credits: int, render_id: Optional[str] = None) -> None:
    allowed, reason = check_usage_with_org(user, SERVICE, count=credits)
    if not allowed:
        left = _credits_left(user["email"])
        extra = f" This needs {credits} credit{'s' if credits != 1 else ''}" + (
            f" and you have {left} left this month." if left is not None else "."
        )
        raise HTTPException(429, reason + extra)
    add_charge(job_id, kind, credits, render_id)


def _refund(user_email: str, job_id: str, kind: str, render_id: Optional[str] = None) -> None:
    credits = mark_charge_refunded(job_id, kind, render_id)
    if credits:
        user = get_user_by_email(user_email)
        if user:
            refund_usage_with_org(user, SERVICE, credits)
        _log.info("Refunded %s credits (%s) for video variant job %s", credits, kind, job_id)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ") if s else None
    except ValueError:
        return None


def _timed_out(started: Optional[str], minutes: int) -> bool:
    t = _parse_iso(started)
    return bool(t and datetime.utcnow() - t > timedelta(minutes=minutes))


def _safe_email(email: str) -> str:
    return email.replace("@", "_at_").replace("/", "_")


def _video_type(content_type: Optional[str], filename: Optional[str]) -> tuple:
    """(content type, extension) of an upload, or 400 if it isn't a supported video."""
    ct = (content_type or "").split(";")[0].strip()
    ext = (os.path.splitext(filename or "")[1].lstrip(".") or "mp4").lower()
    if not ct.startswith("video/") or ext not in vg.VIDEO_EXTENSIONS:
        raise HTTPException(400, "Upload a video file (MP4, MOV or WebM).")
    return ct, ext


def _staging_name(email: str, upload_id: str, ext: str) -> str:
    return f"video_variants/staging/{_safe_email(email)}/{upload_id}/source.{ext}"


def _get_job_or_404(job_id: str, user: dict) -> dict:
    doc = get_video_variant_job(job_id, user["email"])
    if not doc:
        raise HTTPException(404, "Job not found")
    return doc


def _refresh_analysis(doc: dict) -> dict:
    """Advance an analysing job from the engine's progress files; fail + refund on error/timeout."""
    if doc["status"] != "analysing" or not doc.get("video_id"):
        return doc
    try:
        st = vg.get_status(doc["video_id"])
    except Exception as exc:
        _log.warning("Status check failed for %s: %s", doc["job_id"], exc)
        return doc
    if st["status"] == "ready":
        update_job(doc["job_id"], {"status": "ready", "ready_at": now_iso(), "analysis_step": "ready"})
        doc.update(status="ready", analysis_step="ready")
    elif st["status"] == "failed":
        set_job_failed(doc["job_id"], f"Analysis failed: {st['error']}")
        _refund(doc["user_email"], doc["job_id"], "analysis")
        doc.update(status="failed", error=f"Analysis failed: {st['error']}")
    elif _timed_out(doc.get("analysis_started_at"), settings.VIDEO_ANALYSIS_TIMEOUT_MINUTES):
        msg = "Analysis took too long and was stopped. Your credits were refunded."
        set_job_failed(doc["job_id"], msg)
        _refund(doc["user_email"], doc["job_id"], "analysis")
        doc.update(status="failed", error=msg)
    else:
        if st["status"] != doc.get("analysis_step"):
            update_job(doc["job_id"], {"analysis_step": st["status"]})
        doc["analysis_step"] = st["status"]
    return doc


def _render_public(r: dict, sign: bool) -> dict:
    out = {k: r.get(k) for k in (
        "render_id", "status", "created_at", "completed_at", "formats", "variants", "credits", "error"
    )}
    raw = r.get("files") or []
    urls = []
    if sign and raw:
        bucket = vg.gcs.engine_bucket()
        urls = vg.gcs.sign_many(
            [(bucket, f["blob"], None) for f in raw] + [(bucket, f["blob"], f.get("download_name")) for f in raw]
        )
    files = []
    for i, f in enumerate(raw):
        item = {k: f.get(k) for k in ("variant_index", "title", "format", "duration_s", "abcd_job_id")}
        item["index"] = i
        if urls:
            item["url"], item["download_url"] = urls[i], urls[len(raw) + i]
        files.append(item)
    out["files"] = files
    return out


def _to_response(doc: dict, detail: bool = False) -> dict:
    resp = {
        "job_id": doc["job_id"],
        "status": doc["status"],
        "analysis_step": doc.get("analysis_step"),
        "progress": vg.STATUS_PROGRESS.get(doc.get("analysis_step") or "", 5 if doc["status"] == "uploading" else 0),
        "created_at": doc["created_at"],
        "original_filename": doc.get("original_filename"),
        "source": doc.get("source"),
        "parent_abcd_job_id": doc.get("parent_abcd_job_id"),
        "duration_s": doc.get("duration_s"),
        "width": doc.get("width"),
        "height": doc.get("height"),
        "downscaled": bool(doc.get("downscaled")),
        "engine_width": doc.get("engine_width"),
        "engine_height": doc.get("engine_height"),
        "brief": doc.get("brief") or "",
        "objective": doc.get("objective") or "awareness",
        "error": doc.get("error"),
        "credits_used": sum(c["credits"] for c in doc.get("charges") or [] if not c.get("refunded")),
        "render_count": len(doc.get("renders") or []),
    }
    if detail:
        ready = doc["status"] == "ready"
        resp["suggestions"] = doc.get("suggestions") or []
        resp["segments"] = vg.get_segments(doc["video_id"]) if ready else []
        resp["source_url"] = vg.source_video_url(doc["video_id"]) if ready else None
        resp["renders"] = [
            _render_public(r, sign=ready and r.get("status") == "done") for r in reversed(doc.get("renders") or [])
        ]
    return resp


def _start_analysis(job_id: str, user_email: str, source_uri: str, original_filename: str, user_ref: str,
                    local_path: Optional[str] = None) -> None:
    """Put the video in the engine bucket (1080p max), which starts ViGenAiR's analysis."""
    try:
        res = vg.import_video(source_uri, original_filename, user_ref, local_path=local_path)
        update_job(job_id, {
            "status": "analysing", "analysis_step": "uploaded", "video_id": res["video_id"],
            "analysis_started_at": now_iso(), "downscaled": res["downscaled"],
            "engine_width": res["engine_width"], "engine_height": res["engine_height"],
        })
    except vg.VideoTooLongError as exc:
        set_job_failed(job_id, str(exc))
        _refund(user_email, job_id, "analysis")
    except Exception as exc:
        _log.exception("Starting analysis failed for video variant job %s", job_id)
        set_job_failed(job_id, f"Couldn't start the analysis: {exc}")
        _refund(user_email, job_id, "analysis")


def _upload_then_analyse(job_id: str, user_email: str, tmp_path: str, ext: str,
                         content_type: str, original_filename: str, user_ref: str) -> None:
    try:
        if not settings.GCS_BUCKET:
            raise RuntimeError("GCS_BUCKET is not configured")
        name = f"video_variants/{_safe_email(user_email)}/{job_id}/source.{ext}"
        vg.gcs.blob(settings.GCS_BUCKET, name).upload_from_filename(tmp_path, content_type=content_type)
        source_uri = f"gs://{settings.GCS_BUCKET}/{name}"
        update_job(job_id, {"source_uri": source_uri})
    except Exception:
        _log.exception("Upload failed for video variant job %s", job_id)
        set_job_failed(job_id, "Video upload failed. Your credits were refunded.")
        _refund(user_email, job_id, "analysis")
        shutil.rmtree(os.path.dirname(tmp_path), ignore_errors=True)
        return
    try:
        # The temp copy is still here, so a 4K source is converted without downloading it again
        _start_analysis(job_id, user_email, source_uri, original_filename, user_ref, local_path=tmp_path)
    finally:
        shutil.rmtree(os.path.dirname(tmp_path), ignore_errors=True)


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
def get_config(current_user: dict = Depends(get_current_user)):
    """Limits and options for the UI."""
    _check_access(current_user)
    return {
        "max_video_seconds": settings.MAX_VIDEO_SECONDS,
        "max_upload_mb": settings.MAX_VIDEO_UPLOAD_MB,
        "credit_seconds": settings.VIDEO_CREDIT_SECONDS,
        "formats": vg.FORMATS,
        "objectives": [
            {"value": k, "label": v["display_name"], "max_score": v["max_score"]}
            for k, v in vg.ABCD_OBJECTIVES.items()
        ],
        "max_cuts_per_render": MAX_CUTS_PER_RENDER,
        "credits_left": _credits_left(current_user["email"]),
    }


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_from_upload(
    file: UploadFile = File(...),
    brief: str = Form(""),
    objective: str = Form("awareness"),
    current_user: dict = Depends(get_current_user),
):
    """Upload a video (≤ 120 s). Charges ceil(duration / 30) credits, then starts the analysis."""
    _check_access(current_user)
    ct, ext = _video_type(file.content_type, file.filename)

    # Save to a temp file (streamed, size-capped) and measure it before charging anything
    tmp_dir = tempfile.mkdtemp(prefix="vv_")
    tmp_path = os.path.join(tmp_dir, f"source.{ext}")
    max_bytes = settings.MAX_VIDEO_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(400, f"File is too large (max {settings.MAX_VIDEO_UPLOAD_MB} MB).")
                out.write(chunk)
        try:
            info = await run_in_threadpool(vg.probe_file, tmp_path)
            vg.check_length(info)
        except vg.VideoTooLongError as exc:
            raise HTTPException(400, str(exc))
        except vg.ProbeError:
            raise HTTPException(400, "Couldn't read this video. Try exporting it again as MP4 (H.264).")
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    credits = vg.credits_for(info["duration_s"])
    job_id = create_video_variant_job(
        user_email=current_user["email"],
        original_filename=file.filename or f"video.{ext}",
        source="upload",
        brief=(brief or "").strip()[:1000],
        objective=objective if objective in vg.ABCD_OBJECTIVES else "awareness",
    )
    update_job(job_id, {"duration_s": round(info["duration_s"], 2), "width": info["width"], "height": info["height"]})
    try:
        _charge(current_user, job_id, "analysis", credits)
    except HTTPException:
        delete_video_variant_job(job_id, current_user["email"])  # nothing ran; don't clutter History
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # Inside the request, not a BackgroundTask: abcd-backend runs with Cloud Run CPU throttling,
    # so work after the response gets almost no CPU. A 4K → 1080p conversion takes ~15–60 s.
    await run_in_threadpool(
        _upload_then_analyse, job_id, current_user["email"], tmp_path, ext,
        ct or "video/mp4", file.filename or f"video.{ext}", str(current_user["_id"]),
    )
    return _to_response(get_video_variant_job(job_id, current_user["email"]))


# Direct upload (what the UI uses): Cloud Run rejects request bodies over 32 MB, so the
# browser PUTs the file straight to GCS with a signed URL, then calls /from-upload.
# POST "" above (multipart through the backend) stays for local use and small files.

class UploadUrlBody(BaseModel):
    filename: str = Field(..., min_length=1, max_length=300)
    content_type: str = Field(..., max_length=100)
    size_bytes: int = Field(..., gt=0)


@router.post("/upload-url")
def create_upload_url(body: UploadUrlBody, current_user: dict = Depends(get_current_user)):
    """Step 1: a signed URL the browser PUTs the video to (same Content-Type). Nothing is charged."""
    _check_access(current_user)
    ct, ext = _video_type(body.content_type, body.filename)
    if body.size_bytes > settings.MAX_VIDEO_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"File is too large (max {settings.MAX_VIDEO_UPLOAD_MB} MB).")
    if not settings.GCS_BUCKET:
        raise HTTPException(503, "Video upload isn't configured on the server (GCS_BUCKET).")
    upload_id = uuid4().hex
    name = _staging_name(current_user["email"], upload_id, ext)
    try:
        url = vg.gcs.signed_upload_url(settings.GCS_BUCKET, name, ct)
    except Exception as exc:
        _log.exception("Signing upload URL failed")
        raise HTTPException(503, f"Couldn't prepare the upload: {exc}")
    return {"upload_id": upload_id, "upload_url": url, "content_type": ct}


class FromUploadBody(BaseModel):
    upload_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    filename: str = Field(..., min_length=1, max_length=300)
    brief: str = Field("", max_length=1000)
    objective: str = "awareness"


@router.post("/from-upload", status_code=201)
def create_from_direct_upload(body: FromUploadBody, current_user: dict = Depends(get_current_user)):
    """Step 2: measure the uploaded file, reject > 120 s, charge, then start the analysis."""
    _check_access(current_user)
    ext = (os.path.splitext(body.filename)[1].lstrip(".") or "mp4").lower()
    if ext not in vg.VIDEO_EXTENSIONS:
        raise HTTPException(400, "Upload a video file (MP4, MOV or WebM).")
    if not settings.GCS_BUCKET:
        raise HTTPException(503, "Video upload isn't configured on the server (GCS_BUCKET).")
    name = _staging_name(current_user["email"], body.upload_id, ext)
    uri = f"gs://{settings.GCS_BUCKET}/{name}"
    b = vg.gcs.get_blob(settings.GCS_BUCKET, name)
    if b is None:
        raise HTTPException(400, "The upload didn't arrive. Please try again.")

    def reject(msg: str):
        try:
            b.delete()
        except Exception:
            pass
        raise HTTPException(400, msg)

    if (b.size or 0) > settings.MAX_VIDEO_UPLOAD_MB * 1024 * 1024:
        reject(f"File is too large (max {settings.MAX_VIDEO_UPLOAD_MB} MB).")
    try:
        info = vg.probe_gcs(uri)
        vg.check_length(info)
    except vg.VideoTooLongError as exc:
        reject(str(exc))
    except vg.ProbeError:
        reject("Couldn't read this video. Try exporting it again as MP4 (H.264).")

    job_id = create_video_variant_job(
        user_email=current_user["email"],
        original_filename=body.filename,
        source="upload",
        brief=body.brief.strip(),
        objective=body.objective if body.objective in vg.ABCD_OBJECTIVES else "awareness",
    )
    update_job(job_id, {
        "source_uri": uri, "duration_s": round(info["duration_s"], 2),
        "width": info["width"], "height": info["height"],
    })
    try:
        _charge(current_user, job_id, "analysis", vg.credits_for(info["duration_s"]))
    except HTTPException:
        delete_video_variant_job(job_id, current_user["email"])
        raise
    # Inside the request (Cloud Run CPU throttling, see create_from_upload)
    _start_analysis(job_id, current_user["email"], uri, body.filename, str(current_user["_id"]))
    return _to_response(get_video_variant_job(job_id, current_user["email"]))


class FromAbcdBody(BaseModel):
    abcd_job_id: str


@router.post("/from-abcd", status_code=201)
def create_from_abcd(body: FromAbcdBody, current_user: dict = Depends(get_current_user)):
    """Start from an ABCD Analyzer job's uploaded video (no re-upload). Brief/objective are pre-filled."""
    _check_access(current_user)
    abcd = jobs_collection.find_one({"_id": body.abcd_job_id, "user_email": current_user["email"]})
    if not abcd:
        raise HTTPException(404, "Analyzer job not found")
    uri = abcd.get("video_identifier") or ""
    if not uri.startswith("gs://"):
        raise HTTPException(400, "Only uploaded videos can be used (not YouTube links).")
    try:
        info = vg.probe_gcs(uri)
        vg.check_length(info)
    except vg.VideoTooLongError as exc:
        raise HTTPException(400, str(exc))
    except vg.ProbeError as exc:
        raise HTTPException(400, f"Couldn't read this video: {exc}")

    brief_parts = []
    if abcd.get("brand_name") and abcd["brand_name"] != "My Brand":
        brief_parts.append(f"Brand: {abcd['brand_name']}.")
    if abcd.get("products"):
        brief_parts.append(f"Focus on the product: {', '.join(abcd['products'])}.")
    if abcd.get("call_to_actions"):
        brief_parts.append(f"Keep the call to action: {', '.join(abcd['call_to_actions'])}.")
    objective = OBJECTIVE_FROM_ABCD.get(abcd.get("objective") or "", "awareness")
    if abcd.get("creative_format") == "shorts":
        objective = "shorts"

    filename = os.path.basename(uri.split("/", 3)[-1]) or "video.mp4"
    job_id = create_video_variant_job(
        user_email=current_user["email"],
        original_filename=filename,
        source="abcd",
        parent_abcd_job_id=body.abcd_job_id,
        brief=" ".join(brief_parts),
        objective=objective,
    )
    update_job(job_id, {
        "source_uri": uri, "duration_s": round(info["duration_s"], 2),
        "width": info["width"], "height": info["height"],
    })
    try:
        _charge(current_user, job_id, "analysis", vg.credits_for(info["duration_s"]))
    except HTTPException:
        delete_video_variant_job(job_id, current_user["email"])
        raise
    # Inside the request (Cloud Run CPU throttling, see create_from_upload); a source above
    # 1080p is converted first, which can take up to a minute.
    _start_analysis(job_id, current_user["email"], uri, filename, str(current_user["_id"]))
    return _to_response(get_video_variant_job(job_id, current_user["email"]))


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_jobs(limit: int = 50, current_user: dict = Depends(get_current_user)):
    _check_access(current_user)
    return [_to_response(_refresh_analysis(d)) for d in list_video_variant_jobs(current_user["email"], min(limit, 100))]


@router.get("/{job_id}")
def get_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Poll every 5 s while status is uploading/analysing. Includes scenes once ready."""
    _check_access(current_user)
    doc = _refresh_analysis(_get_job_or_404(job_id, current_user))
    try:
        return _to_response(doc, detail=True)
    except vg.VigenairConfigError as exc:
        raise HTTPException(503, f"Video Variants isn't configured on the server: {exc}")


# ── Suggestions ───────────────────────────────────────────────────────────────

class SuggestBody(BaseModel):
    target_s: float = Field(..., gt=0)
    brief: str = Field("", max_length=1000)
    objective: str = "awareness"


@router.post("/{job_id}/suggestions")
def suggest_cuts(job_id: str, body: SuggestBody, current_user: dict = Depends(get_current_user)):
    """Gemini-suggested cuts (takes ~10–30 s). Free; capped per video."""
    _check_access(current_user)
    doc = _get_job_or_404(job_id, current_user)
    if doc["status"] != "ready":
        raise HTTPException(409, "The video is still being analysed.")
    if (doc.get("generation_count") or 0) >= MAX_GENERATIONS_PER_VIDEO:
        raise HTTPException(429, "Suggestion limit reached for this video. Pick scenes manually instead.")
    if body.target_s >= (doc.get("duration_s") or 0):
        raise HTTPException(400, "Target length must be shorter than the video.")
    objective = body.objective if body.objective in vg.ABCD_OBJECTIVES else "awareness"
    try:
        suggestions = vg.generate_variants(doc["video_id"], body.target_s, body.brief.strip(), objective)
    except vg.GenerationError as exc:
        raise HTTPException(502, str(exc))
    update_job(job_id, {
        "suggestions": suggestions, "brief": body.brief.strip(), "objective": objective,
        "generation_count": (doc.get("generation_count") or 0) + 1,
    })
    return {"suggestions": suggestions}


# ── Render ────────────────────────────────────────────────────────────────────

class CutIn(BaseModel):
    title: str = Field("", max_length=200)
    scenes: List[str] = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=4000)
    score: float = 0
    max_score: Optional[int] = None
    abcd: str = Field("", max_length=12000)


class RenderBody(BaseModel):
    variants: List[CutIn] = Field(..., min_length=1, max_length=MAX_CUTS_PER_RENDER)
    formats: List[str] = Field(..., min_length=1, max_length=5)
    fade_out: bool = True


def _quote(doc: dict, body: RenderBody) -> dict:
    if doc["status"] != "ready":
        raise HTTPException(409, "The video is still being analysed.")
    try:
        return vg.quote_render(doc["video_id"], [v.model_dump() for v in body.variants], body.formats)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{job_id}/renders:quote")
def quote_render(job_id: str, body: RenderBody, current_user: dict = Depends(get_current_user)):
    """What a render would produce and cost. Nothing is charged."""
    _check_access(current_user)
    q = _quote(_get_job_or_404(job_id, current_user), body)
    q["credits_left"] = _credits_left(current_user["email"])
    return q


@router.post("/{job_id}/renders", status_code=201)
def start_render(job_id: str, body: RenderBody, current_user: dict = Depends(get_current_user)):
    """Re-quotes server-side (never trusts the browser's number), charges, then starts the render."""
    _check_access(current_user)
    doc = _get_job_or_404(job_id, current_user)
    if any(r.get("status") == "rendering" for r in doc.get("renders") or []):
        raise HTTPException(409, "A render for this video is already running. Wait for it to finish.")
    q = _quote(doc, body)
    pending_id = f"pending-{now_iso()}"
    _charge(current_user, job_id, "render", q["credits"], pending_id)
    try:
        render_id = vg.start_render(doc["video_id"], [v.model_dump() for v in body.variants], body.formats, body.fade_out)
    except Exception as exc:
        _log.exception("Starting render failed for %s", job_id)
        _refund(current_user["email"], job_id, "render", pending_id)
        raise HTTPException(502, f"Couldn't start the render: {exc}")
    rekey_charge(job_id, pending_id, render_id)
    render = {
        "render_id": render_id,
        "status": "rendering",
        "created_at": now_iso(),
        "completed_at": None,
        "formats": body.formats,
        "variants": [
            {"title": v.title or f"Cut {i + 1}", "scenes": v.scenes, "score": v.score, "max_score": v.max_score,
             "duration_s": next(f["duration_s"] for f in q["files"] if f["variant_index"] == i)}
            for i, v in enumerate(body.variants)
        ],
        "credits": q["credits"],
        "files": [],
        "error": None,
    }
    push_render(job_id, render)
    out = _render_public(render, sign=False)
    out["credits_left"] = _credits_left(current_user["email"])
    return out


@router.get("/{job_id}/renders/{render_id}")
def get_render(job_id: str, render_id: str, current_user: dict = Depends(get_current_user)):
    """Poll every 5 s while status = rendering. Done → files with signed play/download URLs."""
    _check_access(current_user)
    doc = _get_job_or_404(job_id, current_user)
    r = next((x for x in doc.get("renders") or [] if x["render_id"] == render_id), None)
    if not r:
        raise HTTPException(404, "Render not found")
    if r["status"] == "rendering":
        res = vg.get_render(doc["video_id"], render_id)
        if res["status"] == "done" and res["files"]:
            durations = {i: v["duration_s"] for i, v in enumerate(r.get("variants") or [])}
            files = [
                {**f, "duration_s": durations.get(f["variant_index"]), "abcd_job_id": None}
                for f in res["files"]
            ]
            update_render(job_id, render_id, {"status": "done", "completed_at": now_iso(), "files": files})
            r.update(status="done", completed_at=now_iso(), files=files)
        elif _timed_out(r.get("created_at"), settings.VIDEO_RENDER_TIMEOUT_MINUTES):
            msg = "Rendering took too long and was stopped. Your credits were refunded."
            update_render(job_id, render_id, {"status": "failed", "error": msg, "completed_at": now_iso()})
            _refund(current_user["email"], job_id, "render", render_id)
            r.update(status="failed", error=msg)
        else:
            r["variants_done"] = res.get("variants_done", 0)
    out = _render_public(r, sign=r["status"] == "done")
    out["variants_done"] = r.get("variants_done")
    return out


# ── Score a rendered file with the ABCD Analyzer ─────────────────────────────

class ScoreBody(BaseModel):
    brand_name: Optional[str] = Field(None, max_length=200)


@router.post("/{job_id}/renders/{render_id}/files/{file_index}/score", status_code=201)
def score_file(
    job_id: str, render_id: str, file_index: int, background_tasks: BackgroundTasks,
    body: Optional[ScoreBody] = None, current_user: dict = Depends(get_current_user),
):
    """Run a normal ABCD Analyzer job on one rendered file (counts as 1 Analyzer run)."""
    _check_access(current_user)
    if "admin" not in (current_user.get("roles") or []) and "abcd_analyzer" not in (current_user.get("allowed_services") or []):
        raise HTTPException(403, "Scoring needs access to the ABCD Analyzer.")
    doc = _get_job_or_404(job_id, current_user)
    r = next((x for x in doc.get("renders") or [] if x["render_id"] == render_id), None)
    if not r or r.get("status") != "done":
        raise HTTPException(404, "Render not found or not finished")
    files = r.get("files") or []
    if not 0 <= file_index < len(files):
        raise HTTPException(404, "File not found")
    f = files[file_index]
    if f.get("abcd_job_id"):
        return {"abcd_job_id": f["abcd_job_id"]}

    allowed, reason = check_usage_with_org(current_user, "abcd_analyzer")
    if not allowed:
        raise HTTPException(429, reason)

    from job_repository import create_job_record, set_job_failed as set_abcd_failed, set_job_video_identifier
    from routers.jobs import _run_job_worker

    parent = jobs_collection.find_one({"_id": doc.get("parent_abcd_job_id")}) if doc.get("parent_abcd_job_id") else None
    brand = (body.brand_name if body and body.brand_name else None) or (parent or {}).get("brand_name") or "My Brand"
    meta = {k: parent[k] for k in ("brand_variations", "products", "product_categories", "call_to_actions", "objective")
            if parent and parent.get(k) is not None}
    meta["creative_format"] = "shorts" if f["format"] == "9:16" else "auto"
    meta["source"] = {"video_variant_job_id": job_id, "render_id": render_id, "format": f["format"], "title": f["title"]}

    abcd_job_id = create_job_record(video_identifier="", brand_name=brand, user_email=current_user["email"],
                                    extra_metadata=meta)
    try:
        dest = f"uploads/{_safe_email(current_user['email'])}/{abcd_job_id}/variant_{f['format'].replace(':', 'x')}.mp4"
        uri = vg.copy_render_file(doc["video_id"], f["blob"], settings.GCS_BUCKET, dest)
        set_job_video_identifier(abcd_job_id, uri)
    except Exception as exc:
        _log.exception("Copying variant for scoring failed")
        set_abcd_failed(abcd_job_id, "Couldn't copy the variant for analysis")
        refund_usage_with_org(current_user, "abcd_analyzer", 1)
        raise HTTPException(502, f"Couldn't prepare the file for scoring: {exc}")
    set_file_abcd_job(job_id, render_id, file_index, abcd_job_id)
    background_tasks.add_task(_run_job_worker, abcd_job_id, uri, brand)
    return {"abcd_job_id": abcd_job_id}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{job_id}")
def delete_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Delete the video and all its renders from storage. Credits already used aren't returned."""
    _check_access(current_user)
    doc = _get_job_or_404(job_id, current_user)
    if doc["status"] in ("uploading", "analysing") or any(r.get("status") == "rendering" for r in doc.get("renders") or []):
        raise HTTPException(409, "Wait until processing finishes before deleting.")
    if doc.get("video_id"):
        vg.delete_video(doc["video_id"])
    delete_video_variant_job(job_id, current_user["email"])
    return {"ok": True}

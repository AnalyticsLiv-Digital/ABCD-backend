"""
Job lifecycle API: create job, get status/result, list jobs.
Phase 1: mock worker. Phase 2: real ABCD when GCP configured.
"""
from datetime import datetime, timedelta, timezone
import logging
import secrets
from typing import Optional, Dict, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

from config import settings
from routers.auth import get_current_user
from schemas import (
    CreateJobRequest,
    CreateJobResponse,
    JobListResponse,
    JobResponse,
    JobStatus,
    JobSummary,
    get_mock_result_payload,
)
from job_repository import (
    create_job_record,
    get_job_response,
    list_job_summaries,
    set_job_completed,
    set_job_failed,
    set_job_running,
    set_job_video_identifier,
)
from db import jobs_collection
from pydantic import BaseModel
from google.cloud import storage


router = APIRouter(prefix="/jobs", tags=["jobs"])
_log = logging.getLogger(__name__)
_storage_client = None


def _check_access(user: dict) -> None:
    if "admin" in (user.get("roles") or []):
        return
    services = user.get("allowed_services") or []
    if "abcd_analyzer" not in services:
        raise HTTPException(
            status_code=403,
            detail="Your account does not have access to ABCD Analyzer. Contact an admin.",
        )


def _get_storage_client():
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def _extract_abcd_metadata_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Extract optional brand/campaign/advanced fields from a job document."""
    return {
        "brand_variations": doc.get("brand_variations"),
        "products": doc.get("products"),
        "product_categories": doc.get("product_categories"),
        "call_to_actions": doc.get("call_to_actions"),
        "campaign_name": doc.get("campaign_name"),
        "campaign_tags": doc.get("campaign_tags"),
        "creative_format": doc.get("creative_format"),
        "objective": doc.get("objective"),
        "advanced": doc.get("advanced"),
    }


def _run_mock_worker(job_id: str, video_uri: str, brand_name: str) -> None:
    """Simulate ABCD analysis: delay then set completed with mock result."""
    import time
    time.sleep(settings.MOCK_JOB_DELAY_SECONDS)
    set_job_running(job_id)
    result = get_mock_result_payload(video_uri, brand_name)
    set_job_completed(job_id, result)


def _run_real_abcd_worker(job_id: str, video_uri: str, brand_name: str) -> None:
    """Run real ABCD detector; set job completed or failed."""
    from abcd_service import (
        run_abcd_analysis,
        is_real_abcd_available,
        AbcdConfigError,
        AbcdExternalServiceError,
        AbcdEngineError,
    )

    set_job_running(job_id)
    try:
        if not is_real_abcd_available():
            _log.warning("Real ABCD requested but GCP not configured; falling back to mock")
            _run_mock_worker(job_id, video_uri, brand_name)
            return

        # Load stored metadata for this job so we can pass richer context to ABCD.
        doc = jobs_collection.find_one({"_id": job_id})
        metadata: Dict[str, Any] = _extract_abcd_metadata_from_doc(doc or {})

        try:
            result = run_abcd_analysis(
                video_uri=video_uri,
                brand_name=brand_name,
                brand_variations=metadata.get("brand_variations"),
                products=metadata.get("products"),
                product_categories=metadata.get("product_categories"),
                call_to_actions=metadata.get("call_to_actions"),
                creative_format=metadata.get("creative_format"),
                advanced=metadata.get("advanced"),
            )
        except AbcdConfigError as exc:
            msg = f"Configuration error while preparing analysis: {exc}"
            _log.warning("ABCD config error for job %s: %s", job_id, msg)
            set_job_failed(job_id, msg)
            return
        except AbcdExternalServiceError as exc:
            msg = f"Google Cloud service error during analysis: {exc}"
            _log.warning("ABCD external service error for job %s: %s", job_id, msg)
            set_job_failed(job_id, msg)
            return
        except AbcdEngineError as exc:
            msg = f"Internal ABCD engine error: {exc}"
            _log.error("ABCD engine error for job %s: %s", job_id, msg)
            set_job_failed(job_id, msg)
            return

        set_job_completed(job_id, result)
    except Exception as e:
        _log.exception("ABCD analysis failed for job %s", job_id)
        set_job_failed(job_id, f"Unexpected analysis error: {e}")


def _run_job_worker(job_id: str, video_uri: str, brand_name: str) -> None:
    """Run either real ABCD (if GCP configured) or mock."""
    from abcd_service import is_real_abcd_available

    if is_real_abcd_available():
        _run_real_abcd_worker(job_id, video_uri, brand_name)
    else:
        _run_mock_worker(job_id, video_uri, brand_name)


def _upload_video_to_gcs(job_id: str, user_email: str, upload_file: UploadFile) -> str:
    """Upload the given video file to GCS and return its gs:// URI."""
    if not settings.GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET is not configured")
    bucket = _get_storage_client().bucket(settings.GCS_BUCKET)
    filename = upload_file.filename or "video.mp4"
    safe_email = user_email.replace("@", "_at_").replace("/", "_")
    blob_name = f"uploads/{safe_email}/{job_id}/{filename}"
    blob = bucket.blob(blob_name)
    upload_file.file.seek(0)
    blob.upload_from_file(upload_file.file, content_type=upload_file.content_type)
    return f"gs://{settings.GCS_BUCKET}/{blob_name}"


class ShareRequest(BaseModel):
    enable: bool
    expires_in_days: Optional[int] = None


class ShareResponse(BaseModel):
    share_url: Optional[str]


@router.post("", response_model=CreateJobResponse, status_code=201)
async def create_job(
    request: CreateJobRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
) -> CreateJobResponse:
    """Create a new analysis job. One of youtube_url or video_url must be provided."""
    _check_access(current_user)

    video_uri = request.youtube_url or request.video_url
    if not video_uri:
        raise HTTPException(
            status_code=400,
            detail="Provide either youtube_url or video_url",
        )
    brand_name = request.brand_name or "My Brand"

    # Enforce per-user usage limits
    from user_repository import check_and_increment_service_usage

    if not check_and_increment_service_usage(current_user, "abcd_analyzer"):
        raise HTTPException(
            status_code=429,
            detail="Monthly usage limit reached for ABCD Analyzer. Contact an admin to increase your limit.",
        )
    # Prepare extra metadata to persist with the job document so workers/ABCD can use it.
    extra_metadata: Dict[str, Any] = {}
    if request.brand_variations is not None:
        extra_metadata["brand_variations"] = request.brand_variations
    if request.products is not None:
        extra_metadata["products"] = request.products
    if request.product_categories is not None:
        extra_metadata["product_categories"] = request.product_categories
    if request.call_to_actions is not None:
        extra_metadata["call_to_actions"] = request.call_to_actions
    if request.campaign_name is not None:
        extra_metadata["campaign_name"] = request.campaign_name
    if request.campaign_tags is not None:
        extra_metadata["campaign_tags"] = request.campaign_tags
    if request.creative_format is not None:
        extra_metadata["creative_format"] = request.creative_format
    if request.objective is not None:
        extra_metadata["objective"] = request.objective
    if request.advanced is not None:
        # Store as plain dict so workers don't depend on Pydantic models.
        extra_metadata["advanced"] = request.advanced.model_dump()

    try:
        job_id = create_job_record(
            video_identifier=video_uri,
            brand_name=brand_name,
            user_email=current_user["email"],
            extra_metadata=extra_metadata or None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("Failed to create job record: %s", exc)
        raise HTTPException(status_code=500, detail="Internal error while creating job")

    background_tasks.add_task(_run_job_worker, job_id, video_uri, brand_name)

    return CreateJobResponse(job_id=job_id, status=JobStatus.PENDING)


def _parse_csv_field(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",")]
    cleaned = [p for p in parts if p]
    return cleaned or None


@router.post("/upload", response_model=CreateJobResponse, status_code=201)
async def upload_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    brand_name: str = Form("My Brand"),
    # Optional brand/campaign metadata as comma-separated strings
    brand_variations: Optional[str] = Form(None),
    products: Optional[str] = Form(None),
    product_categories: Optional[str] = Form(None),
    call_to_actions: Optional[str] = Form(None),
    campaign_name: Optional[str] = Form(None),
    campaign_tags: Optional[str] = Form(None),
    creative_format: Optional[str] = Form(None),  # "long_form" | "shorts" | "auto"
    objective: Optional[str] = Form(None),  # "awareness" | "consideration" | "conversion" | "other"
    # Advanced toggles (bool-like form fields)
    advanced_enable_llms: Optional[bool] = Form(None),
    advanced_enable_annotations: Optional[bool] = Form(None),
    advanced_allow_public_share: Optional[bool] = Form(None),
    current_user: dict = Depends(get_current_user),
) -> CreateJobResponse:
    """Create a new analysis job from an uploaded video file (GCS + ABCD)."""
    _check_access(current_user)

    # Basic validation
    if not file.content_type or "mp4" not in file.content_type:
        raise HTTPException(status_code=400, detail="Only MP4 video uploads are supported.")

    from user_repository import check_and_increment_service_usage

    if not check_and_increment_service_usage(current_user, "abcd_analyzer"):
        raise HTTPException(
            status_code=429,
            detail="Monthly usage limit reached for ABCD Analyzer. Contact an admin to increase your limit.",
        )

    extra_metadata: Dict[str, Any] = {}
    bv = _parse_csv_field(brand_variations)
    if bv is not None:
        extra_metadata["brand_variations"] = bv
    prods = _parse_csv_field(products)
    if prods is not None:
        extra_metadata["products"] = prods
    cats = _parse_csv_field(product_categories)
    if cats is not None:
        extra_metadata["product_categories"] = cats
    ctas = _parse_csv_field(call_to_actions)
    if ctas is not None:
        extra_metadata["call_to_actions"] = ctas
    if campaign_name is not None:
        extra_metadata["campaign_name"] = campaign_name
    tags = _parse_csv_field(campaign_tags)
    if tags is not None:
        extra_metadata["campaign_tags"] = tags
    if creative_format is not None:
        extra_metadata["creative_format"] = creative_format
    if objective is not None:
        extra_metadata["objective"] = objective

    advanced: Dict[str, Any] = {}
    if advanced_enable_llms is not None:
        advanced["enable_llms"] = bool(advanced_enable_llms)
    if advanced_enable_annotations is not None:
        advanced["enable_annotations"] = bool(advanced_enable_annotations)
    if advanced_allow_public_share is not None:
        advanced["allow_public_share"] = bool(advanced_allow_public_share)
    if advanced:
        extra_metadata["advanced"] = advanced

    # Create job record first (video_identifier will be set after upload)
    try:
        job_id = create_job_record(
            video_identifier="",
            brand_name=brand_name or "My Brand",
            user_email=current_user["email"],
            extra_metadata=extra_metadata or None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("Failed to create job record for upload: %s", exc)
        raise HTTPException(status_code=500, detail="Internal error while creating job")

    # Upload to GCS
    try:
        gcs_uri = _upload_video_to_gcs(job_id, current_user["email"], file)
        set_job_video_identifier(job_id, gcs_uri)
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("Failed to upload video to GCS for job %s: %s", job_id, exc)
        set_job_failed(job_id, "Video upload failed")
        raise HTTPException(status_code=500, detail="Failed to upload video")

    background_tasks.add_task(_run_job_worker, job_id, gcs_uri, brand_name or "My Brand")

    return CreateJobResponse(job_id=job_id, status=JobStatus.PENDING)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, current_user: dict = Depends(get_current_user)) -> JobResponse:
    """Get job status and result (when completed)."""
    response = get_job_response(job_id, user_email=current_user["email"])
    if not response:
        raise HTTPException(status_code=404, detail="Job not found")
    return response


@router.get("", response_model=JobListResponse)
async def list_jobs(limit: int = 20, current_user: dict = Depends(get_current_user)) -> JobListResponse:
    """List recent jobs. Default limit 20."""
    if limit < 1 or limit > 100:
        limit = 20
    try:
        summaries = list_job_summaries(user_email=current_user["email"], limit=limit)
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("Failed to list jobs: %s", exc)
        raise HTTPException(status_code=500, detail="Internal error while listing jobs")
    return JobListResponse(jobs=summaries, total=len(summaries))


@router.post("/{job_id}/share", response_model=ShareResponse)
async def share_job(
    job_id: str,
    body: ShareRequest,
    current_user: dict = Depends(get_current_user),
) -> ShareResponse:
    """Enable/disable a secure public share link for a job owned by the current user."""
    # Ensure the job belongs to the current user
    doc = jobs_collection.find_one({"_id": job_id, "user_email": current_user["email"]})
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")

    now = datetime.now(timezone.utc)

    if body.enable:
        public_key = secrets.token_urlsafe(32)
        exp = None
        if body.expires_in_days:
            exp = now + timedelta(days=body.expires_in_days)

        jobs_collection.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "public_enabled": True,
                    "public_key": public_key,
                    "public_expires_at": exp,
                }
            },
        )
        # Frontend will prefix this with window.location.origin
        share_path = f"/share?job_id={job_id}&key={public_key}"
        return ShareResponse(share_url=share_path)
    else:
        jobs_collection.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "public_enabled": False,
                    "public_key": None,
                    "public_expires_at": None,
                }
            },
        )
        return ShareResponse(share_url=None)

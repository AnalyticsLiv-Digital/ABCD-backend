from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import jobs_collection
from schemas import JobResponse, JobResultPayload, JobStatus, JobSummary


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_job_record(
    video_identifier: str,
    brand_name: str,
    user_email: str,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert a new job document and return its id.

    extra_metadata can include optional brand/campaign fields from CreateJobRequest, for example:
    - brand_variations, products, product_categories, call_to_actions
    - campaign_name, campaign_tags
    - creative_format, objective
    - advanced options (already normalized to primitives)
    """
    job_id = str(uuid4())
    doc: Dict[str, Any] = {
        "_id": job_id,
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "created_at": _now_iso(),
        "completed_at": None,
        "error": None,
        "result": None,
        "video_identifier": video_identifier,
        "brand_name": brand_name,
        "user_email": user_email,
        "public_enabled": False,
        "public_key": None,
        "public_expires_at": None,
    }
    if extra_metadata:
        # Only include keys that are JSON-serializable primitives/lists
        for key, value in extra_metadata.items():
            doc[key] = value
    jobs_collection.insert_one(doc)
    return job_id


def set_job_running(job_id: str) -> None:
    jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"status": JobStatus.RUNNING.value}},
    )


def set_job_completed(job_id: str, result: JobResultPayload) -> None:
    jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": JobStatus.COMPLETED.value,
                "completed_at": _now_iso(),
                "result": result.model_dump(),
                "error": None,
            }
        },
    )


def set_job_failed(job_id: str, error: str) -> None:
    jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": JobStatus.FAILED.value,
                "completed_at": _now_iso(),
                "error": error,
                "result": None,
            }
        },
    )


def set_job_video_identifier(job_id: str, video_identifier: str) -> None:
    """Update the stored video identifier (e.g. after upload to GCS)."""
    jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"video_identifier": video_identifier}},
    )


def _build_job_response(doc: Dict[str, Any]) -> JobResponse:
    result_obj: Optional[JobResultPayload] = None
    if doc.get("result"):
        result_obj = JobResultPayload.model_validate(doc["result"])
    return JobResponse(
        job_id=doc["job_id"],
        status=JobStatus(doc["status"]),
        created_at=doc["created_at"],
        completed_at=doc.get("completed_at"),
        error=doc.get("error"),
        result=result_obj,
        video_identifier=doc.get("video_identifier"),
    )


def get_job_response(job_id: str, user_email: str) -> Optional[JobResponse]:
    doc = jobs_collection.find_one({"_id": job_id, "user_email": user_email})
    if not doc:
        return None
    return _build_job_response(doc)


def list_job_summaries(user_email: str, limit: int = 20) -> List[JobSummary]:
    cursor = jobs_collection.find({"user_email": user_email}, sort=[("created_at", -1)]).limit(limit)
    summaries: List[JobSummary] = []
    for doc in cursor:
        summaries.append(
            JobSummary(
                job_id=doc["job_id"],
                status=JobStatus(doc["status"]),
                created_at=doc["created_at"],
                video_identifier=doc.get("video_identifier"),
            )
        )
    return summaries


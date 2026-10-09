"""
Repository layer for Creative Studio image enhancement jobs.
Each job is scoped to a user (by email) and tracks the async n8n pipeline.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import image_batches_collection, image_jobs_collection


class ImageJobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"   # n8n received the job, working on it
    COMPLETED = "completed"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_image_job_record(
    user_email: str,
    prompt: Optional[str] = None,
    original_filename: Optional[str] = None,
    original_url: Optional[str] = None,
) -> str:
    """Insert a new single (non-batch) image job document and return its job_id."""
    job_id = str(uuid4())
    doc: Dict[str, Any] = {
        "_id": job_id,
        "job_id": job_id,
        "user_email": user_email,
        "status": ImageJobStatus.PENDING.value,
        "created_at": _now_iso(),
        "completed_at": None,
        "prompt": prompt,
        "original_filename": original_filename,
        "original_url": original_url,
        "result_urls": [],
        "error": None,
    }
    image_jobs_collection.insert_one(doc)
    return job_id


def update_original_url(job_id: str, original_url: str) -> None:
    image_jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"original_url": original_url}},
    )


def set_image_job_completed(job_id: str, result_urls: List[str]) -> None:
    image_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": ImageJobStatus.COMPLETED.value,
                "completed_at": _now_iso(),
                "result_urls": result_urls,
                "error": None,
            }
        },
    )


def set_image_job_processing(job_id: str) -> None:
    """Mark job as processing — n8n received it and is working on it."""
    image_jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"status": ImageJobStatus.PROCESSING.value}},
    )


def set_image_job_failed(job_id: str, error: str) -> None:
    image_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": ImageJobStatus.FAILED.value,
                "completed_at": _now_iso(),
                "error": error,
            }
        },
    )


def set_image_job_failed_if_in_flight(job_id: str, error: str) -> bool:
    """Fail a job only if it is still pending/processing (never clobbers a completed result)."""
    result = image_jobs_collection.update_one(
        {"_id": job_id, "status": {"$in": [ImageJobStatus.PENDING.value, ImageJobStatus.PROCESSING.value]}},
        {"$set": {"status": ImageJobStatus.FAILED.value, "completed_at": _now_iso(), "error": error}},
    )
    return result.modified_count == 1


def reset_image_job_for_retry(job_id: str, user_email: str) -> bool:
    """
    Atomically move a failed job (whose original is stored) back to pending for a re-run.
    Returns False if the job is not failed / has no original (e.g. double-click on Retry).
    """
    result = image_jobs_collection.update_one(
        {
            "_id": job_id,
            "user_email": user_email,
            "status": ImageJobStatus.FAILED.value,
            "original_url": {"$nin": [None, ""]},
        },
        {
            "$set": {
                "status": ImageJobStatus.PENDING.value,
                "completed_at": None,
                "result_urls": [],
                "error": None,
                "dispatched_at": _now_iso(),
            },
            "$inc": {"retry_count": 1},
        },
    )
    return result.modified_count == 1


def get_image_job(job_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return image_jobs_collection.find_one({"_id": job_id, "user_email": user_email})


def list_image_jobs(user_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(
        image_jobs_collection.find(
            {"user_email": user_email},
            sort=[("created_at", -1)],
        ).limit(limit)
    )


# ── Batches (multi-image submit) ─────────────────────────────────────────────
# A batch is a thin parent record; each image is still a normal image job (same n8n
# contract, same callback). Batch status is derived from its jobs on read.

def create_image_batch(
    user_email: str,
    prompt: Optional[str],
    filenames: List[str],
) -> Dict[str, Any]:
    """Create a batch and one pending job per file (awaiting upload). Returns the batch doc."""
    batch_id = str(uuid4())
    now = _now_iso()
    job_ids = [str(uuid4()) for _ in filenames]
    batch_doc: Dict[str, Any] = {
        "_id": batch_id,
        "batch_id": batch_id,
        "user_email": user_email,
        "created_at": now,
        "prompt": prompt,
        "job_ids": job_ids,
    }
    image_batches_collection.insert_one(batch_doc)
    image_jobs_collection.insert_many([
        {
            "_id": job_id,
            "job_id": job_id,
            "user_email": user_email,
            "status": ImageJobStatus.PENDING.value,
            "created_at": now,
            "completed_at": None,
            "prompt": prompt,
            "original_filename": filename,
            "original_url": None,
            "result_urls": [],
            "error": None,
            "batch_id": batch_id,
            "batch_index": i,
            "upload_received": False,
        }
        for i, (job_id, filename) in enumerate(zip(job_ids, filenames))
    ])
    return batch_doc


def get_image_batch(batch_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return image_batches_collection.find_one({"_id": batch_id, "user_email": user_email})


def list_image_batch_jobs(batch_id: str, user_email: str) -> List[Dict[str, Any]]:
    return list(
        image_jobs_collection.find(
            {"batch_id": batch_id, "user_email": user_email},
            sort=[("batch_index", 1)],
        )
    )


def claim_batch_upload(job_id: str, user_email: str) -> bool:
    """
    Atomically mark a batch job's file as received. Guarantees each job is dispatched
    to n8n at most once even if the browser re-sends the upload.
    """
    result = image_jobs_collection.update_one(
        {
            "_id": job_id,
            "user_email": user_email,
            "status": ImageJobStatus.PENDING.value,
            "upload_received": False,
        },
        {"$set": {"upload_received": True, "dispatched_at": _now_iso()}},
    )
    return result.modified_count == 1


def fail_unreceived_upload(job_id: str, user_email: str, error: str) -> None:
    """Fail a batch job whose upload was rejected (only if no file was accepted yet)."""
    image_jobs_collection.update_one(
        {
            "_id": job_id,
            "user_email": user_email,
            "status": ImageJobStatus.PENDING.value,
            "upload_received": False,
        },
        {"$set": {"status": ImageJobStatus.FAILED.value, "completed_at": _now_iso(), "error": error}},
    )


# ── Admin (platform-admin only) ──────────────────────────────────────────────

def list_image_jobs_admin(
    user_emails: List[str],
    status: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """List image jobs across the given set of user emails. Bypasses per-user scope."""
    query: Dict[str, Any] = {"user_email": {"$in": user_emails}}
    if status:
        query["status"] = status
    cursor = (
        image_jobs_collection.find(query)
        .sort("created_at", -1)
        .skip(max(0, skip))
        .limit(max(1, limit))
    )
    return [
        {
            "job_id": doc["job_id"],
            "status": doc["status"],
            "created_at": doc["created_at"],
            "completed_at": doc.get("completed_at"),
            "user_email": doc.get("user_email"),
            "prompt": doc.get("prompt"),
            "original_filename": doc.get("original_filename"),
            "original_url": doc.get("original_url"),
            "result_count": len(doc.get("result_urls") or []),
            "error": doc.get("error"),
        }
        for doc in cursor
    ]


def get_image_job_admin(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single image job by id, regardless of owner."""
    return image_jobs_collection.find_one({"_id": job_id})


def get_image_job_owner(job_id: str) -> Optional[str]:
    doc = image_jobs_collection.find_one({"_id": job_id}, {"user_email": 1})
    return doc.get("user_email") if doc else None

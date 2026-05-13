"""
Repository layer for Creatives (aspect-ratio agent) jobs.
Each job is scoped to a user (by email) and tracks the async n8n pipeline.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import creatives_jobs_collection


class CreativesJobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"   # n8n received the job, working on it
    COMPLETED = "completed"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_creatives_job_record(
    user_email: str,
    prompt: Optional[str] = None,
    original_filename: Optional[str] = None,
    original_url: Optional[str] = None,
) -> str:
    """Insert a new creatives job document and return its job_id."""
    job_id = str(uuid4())
    doc: Dict[str, Any] = {
        "_id": job_id,
        "job_id": job_id,
        "user_email": user_email,
        "status": CreativesJobStatus.PENDING.value,
        "created_at": _now_iso(),
        "completed_at": None,
        "prompt": prompt,
        "original_filename": original_filename,
        "original_url": original_url,
        "result_urls": [],
        "error": None,
    }
    creatives_jobs_collection.insert_one(doc)
    return job_id


def update_original_url(job_id: str, original_url: str) -> None:
    creatives_jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"original_url": original_url}},
    )


def set_creatives_job_completed(job_id: str, result_urls: List[str]) -> None:
    creatives_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": CreativesJobStatus.COMPLETED.value,
                "completed_at": _now_iso(),
                "result_urls": result_urls,
                "error": None,
            }
        },
    )


def set_creatives_job_processing(job_id: str) -> None:
    """Mark job as processing — n8n received it and is working on it."""
    creatives_jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"status": CreativesJobStatus.PROCESSING.value}},
    )


def set_creatives_job_failed(job_id: str, error: str) -> None:
    creatives_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": CreativesJobStatus.FAILED.value,
                "completed_at": _now_iso(),
                "error": error,
            }
        },
    )


def get_creatives_job(job_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return creatives_jobs_collection.find_one({"_id": job_id, "user_email": user_email})


def list_creatives_jobs(user_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(
        creatives_jobs_collection.find(
            {"user_email": user_email},
            sort=[("created_at", -1)],
        ).limit(limit)
    )


# ── Admin (platform-admin only) ──────────────────────────────────────────────

def list_creatives_jobs_admin(
    user_emails: List[str],
    status: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """List creatives jobs across the given set of user emails. Bypasses per-user scope."""
    query: Dict[str, Any] = {"user_email": {"$in": user_emails}}
    if status:
        query["status"] = status
    cursor = (
        creatives_jobs_collection.find(query)
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


def get_creatives_job_admin(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single creatives job by id, regardless of owner."""
    return creatives_jobs_collection.find_one({"_id": job_id})


def get_creatives_job_owner(job_id: str) -> Optional[str]:
    doc = creatives_jobs_collection.find_one({"_id": job_id}, {"user_email": 1})
    return doc.get("user_email") if doc else None

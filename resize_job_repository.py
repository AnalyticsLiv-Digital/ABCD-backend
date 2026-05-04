"""
Repository layer for Creative Resize jobs.
Each job tracks an async n8n pipeline that resizes/adapts an image to target formats.

Payload sent to n8n webhook (multipart/form-data):
  - data         : original image file (field name "data")
  - sizes        : JSON string — [{name, width, height}, …]
  - max_size_kb  : integer (KB limit per output image)
  - email        : user email for n8n to deliver results
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import resize_jobs_collection


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_resize_job_record(
    user_email: str,
    original_filename: Optional[str] = None,
    original_url: Optional[str] = None,
    sizes: Optional[List[Dict]] = None,
    max_size_kb: int = 999000,
) -> str:
    """Insert a new resize job document and return its job_id."""
    job_id = str(uuid4())
    doc: Dict[str, Any] = {
        "_id": job_id,
        "job_id": job_id,
        "user_email": user_email,
        "status": "pending",
        "created_at": _now_iso(),
        "completed_at": None,
        "original_filename": original_filename,
        "original_url": original_url,
        "sizes": sizes or [],
        "max_size_kb": max_size_kb,
        "result_urls": [],
        "result_images": [],   # [{url, name, width, height}] — populated on completion
        "error": None,
    }
    resize_jobs_collection.insert_one(doc)
    return job_id


def update_resize_original_url(job_id: str, original_url: str) -> None:
    resize_jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"original_url": original_url}},
    )


def set_resize_job_completed(
    job_id: str,
    result_urls: List[str],
    result_images: Optional[List[Dict]] = None,
) -> None:
    """
    Mark the job completed.

    result_urls  : flat list of GCS URLs (kept for backward-compat)
    result_images: list of {url, name, width, height} dicts — richer metadata
                   used by the UI to show per-image size labels and filenames.
    """
    resize_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "completed",
                "completed_at": _now_iso(),
                "result_urls": result_urls,
                "result_images": result_images or [],
                "error": None,
            }
        },
    )


def set_resize_job_failed(job_id: str, error: str) -> None:
    resize_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "failed",
                "completed_at": _now_iso(),
                "error": error,
            }
        },
    )


def get_resize_job(job_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return resize_jobs_collection.find_one({"_id": job_id, "user_email": user_email})


def list_resize_jobs(user_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(
        resize_jobs_collection.find(
            {"user_email": user_email},
            sort=[("created_at", -1)],
        ).limit(limit)
    )


# ── Admin (platform-admin only) ──────────────────────────────────────────────

def list_resize_jobs_admin(
    user_emails: List[str],
    status: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """List resize jobs across the given set of user emails. Bypasses per-user scope."""
    query: Dict[str, Any] = {"user_email": {"$in": user_emails}}
    if status:
        query["status"] = status
    cursor = (
        resize_jobs_collection.find(query)
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
            "original_filename": doc.get("original_filename"),
            "original_url": doc.get("original_url"),
            "size_count": len(doc.get("sizes") or []),
            "result_count": len(doc.get("result_urls") or []),
            "error": doc.get("error"),
        }
        for doc in cursor
    ]


def get_resize_job_admin(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single resize job by id, regardless of owner."""
    return resize_jobs_collection.find_one({"_id": job_id})


def get_resize_job_owner(job_id: str) -> Optional[str]:
    doc = resize_jobs_collection.find_one({"_id": job_id}, {"user_email": 1})
    return doc.get("user_email") if doc else None

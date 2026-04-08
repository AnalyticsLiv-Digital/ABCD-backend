"""
Repository layer for Creative Resize jobs.
Each job tracks an async n8n pipeline that resizes/adapts an image to a target format.
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
    target_format: Optional[str] = None,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    quality: Optional[str] = "standard",
    fit_mode: Optional[str] = "cover",
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
        "target_format": target_format,
        "target_width": target_width,
        "target_height": target_height,
        "quality": quality,
        "fit_mode": fit_mode,
        "result_urls": [],
        "error": None,
    }
    resize_jobs_collection.insert_one(doc)
    return job_id


def update_resize_original_url(job_id: str, original_url: str) -> None:
    resize_jobs_collection.update_one(
        {"_id": job_id},
        {"$set": {"original_url": original_url}},
    )


def set_resize_job_completed(job_id: str, result_urls: List[str]) -> None:
    resize_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "completed",
                "completed_at": _now_iso(),
                "result_urls": result_urls,
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

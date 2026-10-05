"""
Repository layer for Prompt Creative jobs (brand RAG text-to-image).
Each job is scoped to a user (by email) and tracks the async n8n pipeline.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import prompt_creative_jobs_collection


class PromptCreativeJobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"   # n8n received the job, working on it
    COMPLETED = "completed"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_prompt_creative_job_record(
    user_email: str,
    prompt: str,
    aspect_ratio: Optional[str] = None,
) -> str:
    """Insert a new prompt creative job document and return its job_id."""
    job_id = str(uuid4())
    doc: Dict[str, Any] = {
        "_id": job_id,
        "job_id": job_id,
        "user_email": user_email,
        "status": PromptCreativeJobStatus.PENDING.value,
        "created_at": _now_iso(),
        "completed_at": None,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "result_urls": [],
        "error": None,
    }
    prompt_creative_jobs_collection.insert_one(doc)
    return job_id


def set_prompt_creative_job_completed(job_id: str, result_urls: List[str]) -> None:
    prompt_creative_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": PromptCreativeJobStatus.COMPLETED.value,
                "completed_at": _now_iso(),
                "result_urls": result_urls,
                "error": None,
            }
        },
    )


def set_prompt_creative_job_processing(job_id: str) -> None:
    """Mark job as processing — n8n received it and is working on it."""
    prompt_creative_jobs_collection.update_one(
        {"_id": job_id, "status": PromptCreativeJobStatus.PENDING.value},
        {"$set": {"status": PromptCreativeJobStatus.PROCESSING.value}},
    )


def set_prompt_creative_job_failed(job_id: str, error: str) -> None:
    prompt_creative_jobs_collection.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": PromptCreativeJobStatus.FAILED.value,
                "completed_at": _now_iso(),
                "error": error,
            }
        },
    )


def get_prompt_creative_job(job_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return prompt_creative_jobs_collection.find_one({"_id": job_id, "user_email": user_email})


def get_prompt_creative_job_any(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch by id regardless of owner — used by the n8n callback."""
    return prompt_creative_jobs_collection.find_one({"_id": job_id})


def list_prompt_creative_jobs(user_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(
        prompt_creative_jobs_collection.find(
            {"user_email": user_email},
            sort=[("created_at", -1)],
        ).limit(limit)
    )


# ── Admin (platform-admin only) ──────────────────────────────────────────────

def list_prompt_creative_jobs_admin(
    user_emails: List[str],
    status: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """List prompt creative jobs across the given set of user emails. Bypasses per-user scope."""
    query: Dict[str, Any] = {"user_email": {"$in": user_emails}}
    if status:
        query["status"] = status
    cursor = (
        prompt_creative_jobs_collection.find(query)
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
            "aspect_ratio": doc.get("aspect_ratio"),
            "result_count": len(doc.get("result_urls") or []),
            "error": doc.get("error"),
        }
        for doc in cursor
    ]


def get_prompt_creative_job_admin(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single prompt creative job by id, regardless of owner."""
    return prompt_creative_jobs_collection.find_one({"_id": job_id})

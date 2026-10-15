"""
Repository layer for Creative Agent (one image → many aspect ratios / sizes).

A batch is the parent record for one uploaded image. Each requested output is its own
job (one n8n run, one result image). Batch status is derived from its jobs on read.

Each job carries its own callback token (stored as a SHA-256 hash). The token is sent to
n8n as `callback_secret` and echoed back, so a callback can only complete the job it
was issued for.
"""
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import creative_agent_batches_collection, creative_agent_jobs_collection

PENDING = "pending"         # created, not yet acknowledged by n8n
PROCESSING = "processing"   # n8n acknowledged, working on it
COMPLETED = "completed"
FAILED = "failed"
IN_FLIGHT = [PENDING, PROCESSING]


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Batches ───────────────────────────────────────────────────────────────────

def create_batch(
    user_email: str,
    original_filename: str,
    source_width: Optional[int],
    source_height: Optional[int],
    instructions: Optional[str],
    jobs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Create a batch and one pending job per target.
    `jobs` items: {target: {...}, prompt: str, token_hash: str}.
    Returns the batch doc (with job_ids in target order).
    """
    batch_id = str(uuid4())
    now = _now_iso()
    job_ids = [str(uuid4()) for _ in jobs]
    batch_doc: Dict[str, Any] = {
        "_id": batch_id,
        "batch_id": batch_id,
        "user_email": user_email,
        "created_at": now,
        "original_filename": original_filename,
        "original_url": None,
        "source_width": source_width,
        "source_height": source_height,
        "instructions": instructions,
        "job_ids": job_ids,
    }
    creative_agent_batches_collection.insert_one(batch_doc)
    creative_agent_jobs_collection.insert_many([
        {
            "_id": job_id,
            "job_id": job_id,
            "batch_id": batch_id,
            "index": i,
            "user_email": user_email,
            "target": job["target"],
            "prompt": job["prompt"],
            "status": PENDING,
            "created_at": now,
            "dispatched_at": now,
            "completed_at": None,
            "result_url": None,
            "result_content_type": None,
            "error": None,
            "retry_count": 0,
            "callback_token_hash": job["token_hash"],
        }
        for i, (job_id, job) in enumerate(zip(job_ids, jobs))
    ])
    return batch_doc


def set_batch_original_url(batch_id: str, original_url: str) -> None:
    creative_agent_batches_collection.update_one(
        {"_id": batch_id}, {"$set": {"original_url": original_url}}
    )


def get_batch(batch_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return creative_agent_batches_collection.find_one({"_id": batch_id, "user_email": user_email})


def list_batches(user_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(
        creative_agent_batches_collection.find(
            {"user_email": user_email}, sort=[("created_at", -1)], limit=limit
        )
    )


# ── Jobs ──────────────────────────────────────────────────────────────────────

def list_batch_jobs(batch_ids: List[str], user_email: str) -> List[Dict[str, Any]]:
    """Jobs for one or more batches, ordered by batch then target index."""
    return list(
        creative_agent_jobs_collection.find(
            {"batch_id": {"$in": batch_ids}, "user_email": user_email},
            sort=[("batch_id", 1), ("index", 1)],
        )
    )


def get_job(job_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return creative_agent_jobs_collection.find_one({"_id": job_id, "user_email": user_email})


def get_job_for_callback(job_id: str) -> Optional[Dict[str, Any]]:
    """Owner-agnostic lookup — only for the n8n callback, which authenticates by token."""
    return creative_agent_jobs_collection.find_one({"_id": job_id})


def set_job_processing(job_id: str) -> None:
    """n8n acknowledged the run. Never downgrades a job that already finished."""
    creative_agent_jobs_collection.update_one(
        {"_id": job_id, "status": PENDING},
        {"$set": {"status": PROCESSING}},
    )


def set_job_completed(job_id: str, result_url: str, content_type: str) -> bool:
    """
    Store the result. A late callback still completes a job the timeout already failed,
    but a duplicate callback never overwrites a completed result.
    """
    result = creative_agent_jobs_collection.update_one(
        {"_id": job_id, "status": {"$ne": COMPLETED}},
        {"$set": {
            "status": COMPLETED,
            "completed_at": _now_iso(),
            "result_url": result_url,
            "result_content_type": content_type,
            "error": None,
        }},
    )
    return result.modified_count == 1


def set_job_failed(job_id: str, error: str) -> bool:
    """Fail a job that is still in flight (never clobbers a completed result)."""
    result = creative_agent_jobs_collection.update_one(
        {"_id": job_id, "status": {"$in": IN_FLIGHT}},
        {"$set": {"status": FAILED, "completed_at": _now_iso(), "error": error}},
    )
    return result.modified_count == 1


def reset_job_for_retry(job_id: str, user_email: str, token_hash: str) -> bool:
    """
    Atomically move a failed job back to pending with a fresh callback token, so a late
    callback from the abandoned run can't land on the new one. False on a double-click.
    """
    result = creative_agent_jobs_collection.update_one(
        {"_id": job_id, "user_email": user_email, "status": FAILED},
        {
            "$set": {
                "status": PENDING,
                "dispatched_at": _now_iso(),
                "completed_at": None,
                "result_url": None,
                "result_content_type": None,
                "error": None,
                "callback_token_hash": token_hash,
            },
            "$inc": {"retry_count": 1},
        },
    )
    return result.modified_count == 1


def restore_failed(job_id: str, error: str) -> None:
    """Undo reset_job_for_retry when the retry could not be charged."""
    creative_agent_jobs_collection.update_one(
        {"_id": job_id, "status": PENDING},
        {"$set": {"status": FAILED, "completed_at": _now_iso(), "error": error},
         "$inc": {"retry_count": -1}},
    )

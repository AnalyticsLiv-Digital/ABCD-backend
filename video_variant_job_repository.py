"""
Repository layer for Video Variants (ViGenAiR) jobs.

One document per source video. Renders are embedded (a user typically renders a few times
per video). Every credit charge is recorded in `charges` so a failed step can be refunded
exactly once.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db import video_variant_jobs_collection as col


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_video_variant_job(
    user_email: str,
    original_filename: str,
    source: str,
    parent_abcd_job_id: Optional[str] = None,
    brief: str = "",
    objective: str = "awareness",
) -> str:
    job_id = str(uuid4())
    col.insert_one({
        "_id": job_id,
        "job_id": job_id,
        "user_email": user_email,
        "status": "uploading",          # uploading → analysing → ready | failed
        "created_at": now_iso(),
        "analysis_started_at": None,
        "ready_at": None,
        "original_filename": original_filename,
        "source": source,               # "upload" | "abcd"
        "parent_abcd_job_id": parent_abcd_job_id,
        "source_uri": None,
        "video_id": None,               # ViGenAiR folder name in the engine bucket
        "duration_s": None,
        "width": None,
        "height": None,
        "brief": brief,
        "objective": objective,
        "suggestions": [],
        "generation_count": 0,
        "renders": [],
        "charges": [],
        "error": None,
    })
    return job_id


def get_video_variant_job(job_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    return col.find_one({"_id": job_id, "user_email": user_email})


def list_video_variant_jobs(user_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(col.find({"user_email": user_email}, sort=[("created_at", -1)]).limit(limit))


def update_job(job_id: str, fields: Dict[str, Any]) -> None:
    col.update_one({"_id": job_id}, {"$set": fields})


def set_job_failed(job_id: str, error: str) -> None:
    col.update_one({"_id": job_id}, {"$set": {"status": "failed", "error": error}})


def add_charge(job_id: str, kind: str, credits: int, render_id: Optional[str] = None) -> None:
    col.update_one({"_id": job_id}, {"$push": {"charges": {
        "kind": kind, "render_id": render_id, "credits": int(credits),
        "refunded": False, "at": now_iso(),
    }}})


def mark_charge_refunded(job_id: str, kind: str, render_id: Optional[str] = None) -> Optional[int]:
    """
    Atomically flip one unrefunded charge to refunded. Returns its credits, or None if
    there was nothing to refund (so a refund can never happen twice).
    """
    match = {"kind": kind, "refunded": False}
    if render_id is not None:
        match["render_id"] = render_id
    doc = col.find_one_and_update(
        {"_id": job_id, "charges": {"$elemMatch": match}},
        {"$set": {"charges.$.refunded": True, "charges.$.refunded_at": now_iso()}},
        projection={"charges": {"$elemMatch": match}},
    )
    if not doc or not doc.get("charges"):
        return None
    return int(doc["charges"][0].get("credits") or 0)


def rekey_charge(job_id: str, old_render_id: str, new_render_id: str) -> None:
    col.update_one(
        {"_id": job_id, "charges.render_id": old_render_id},
        {"$set": {"charges.$.render_id": new_render_id}},
    )


def delete_video_variant_job(job_id: str, user_email: str) -> None:
    col.delete_one({"_id": job_id, "user_email": user_email})


def push_render(job_id: str, render: Dict[str, Any]) -> None:
    col.update_one({"_id": job_id}, {"$push": {"renders": render}})


def update_render(job_id: str, render_id: str, fields: Dict[str, Any]) -> None:
    col.update_one(
        {"_id": job_id, "renders.render_id": render_id},
        {"$set": {f"renders.$.{k}": v for k, v in fields.items()}},
    )


def set_file_abcd_job(job_id: str, render_id: str, file_index: int, abcd_job_id: str) -> None:
    doc = col.find_one({"_id": job_id}, {"renders": 1}) or {}
    render = next((r for r in doc.get("renders") or [] if r.get("render_id") == render_id), None)
    if not render or not 0 <= file_index < len(render.get("files") or []):
        return
    files = render["files"]
    files[file_index]["abcd_job_id"] = abcd_job_id
    update_render(job_id, render_id, {"files": files})


# ── Admin (platform-admin only) ──────────────────────────────────────────────

def list_video_variant_jobs_admin(
    user_emails: List[str], status: Optional[str] = None, limit: int = 50, skip: int = 0
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_email": {"$in": user_emails}}
    if status:
        query["status"] = status
    cursor = col.find(query).sort("created_at", -1).skip(max(0, skip)).limit(max(1, limit))
    return [
        {
            "job_id": d["job_id"],
            "status": d["status"],
            "created_at": d["created_at"],
            "user_email": d.get("user_email"),
            "original_filename": d.get("original_filename"),
            "duration_s": d.get("duration_s"),
            "render_count": len(d.get("renders") or []),
            "credits": sum(c["credits"] for c in d.get("charges") or [] if not c.get("refunded")),
            "error": d.get("error"),
        }
        for d in cursor
    ]


def get_video_variant_job_admin(job_id: str) -> Optional[Dict[str, Any]]:
    return col.find_one({"_id": job_id})

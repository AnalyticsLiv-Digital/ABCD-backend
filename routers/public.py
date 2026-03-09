from datetime import datetime

from fastapi import APIRouter, HTTPException

from db import jobs_collection
from job_repository import _build_job_response
from schemas import JobResponse


router = APIRouter(prefix="/public", tags=["public"])


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_public_job(job_id: str, key: str) -> JobResponse:
    """Public read-only access to a single job report via secure key."""
    doc = jobs_collection.find_one({"_id": job_id})
    if not doc or not doc.get("public_enabled"):
        raise HTTPException(status_code=404, detail="Report not available")

    if not key or key != doc.get("public_key"):
        raise HTTPException(status_code=404, detail="Report not available")

    exp = doc.get("public_expires_at")
    if exp:
        # Normalize to naive datetimes for comparison
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except ValueError:
                exp = None
        now = datetime.utcnow()
        if exp and exp < now:
            raise HTTPException(status_code=404, detail="Report link expired")

    return _build_job_response(doc)


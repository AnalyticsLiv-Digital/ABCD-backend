"""
Platform admin endpoints — only accessible to is_platform_admin users (AnalyticsLiv team).

These endpoints manage the multi-tenant layer:
  - Create / update / suspend organizations
  - Set per-org service limits
  - Invite external users to an org
  - View org usage

Org admins (client-side admins) have their own separate endpoints at /org/*.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from routers.auth import get_current_user
from email_service import send_invitation_email
from org_repository import (
    ALL_SERVICES,
    DEFAULT_ORG_SERVICE_LIMIT,
    create_org,
    create_invitation,
    get_org_by_id,
    get_org_usage_history,
    list_orgs,
    list_org_invitations,
    update_org,
)
from user_repository import is_usage_period_stale
from job_repository import (
    get_job_admin as get_abcd_job_admin,
    get_job_owner as get_abcd_job_owner,
    list_jobs_admin as list_abcd_jobs_admin,
)
from image_job_repository import (
    get_image_job_admin,
    list_image_jobs_admin,
)
from resize_job_repository import (
    get_resize_job_admin,
    list_resize_jobs_admin,
)
from db import admin_audit_collection, users_collection, organizations_collection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/platform", tags=["platform-admin"])

VALID_SERVICES = {"abcd_analyzer", "creative_studio", "creative_resize"}
VALID_PLANS = {"starter", "pro", "enterprise"}


# ── Guard ──────────────────────────────────────────────────────────────────────

def _require_platform_admin(current_user: dict) -> dict:
    """Raise 403 if caller is not a platform admin."""
    if not current_user.get("is_platform_admin") and "admin" not in (current_user.get("roles") or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin access required")
    return current_user


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateOrgRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    plan: str = Field("starter")
    allowed_services: List[str] = Field(default_factory=lambda: ["abcd_analyzer"])
    service_limits: Dict[str, int] = Field(default_factory=dict)
    # Domains to whitelist: any Google user from these domains auto-joins this org
    allowed_domains: List[str] = Field(default_factory=list)


class UpdateOrgRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    plan: Optional[str] = None
    allowed_services: Optional[List[str]] = None
    service_limits: Optional[Dict[str, int]] = None
    allowed_domains: Optional[List[str]] = None
    status: Optional[str] = None  # "active" | "suspended"


class InviteUserRequest(BaseModel):
    email: EmailStr
    role: str = Field("member")  # "member" | "admin"
    expires_hours: int = Field(72, ge=1, le=720)


class OrgResponse(BaseModel):
    id: str
    name: str
    slug: str
    plan: str
    status: str
    allowed_services: List[str]
    service_limits: Dict[str, int]
    service_usage: Dict[str, int]
    allowed_domains: List[str]
    created_at: Optional[str] = None
    user_count: int = 0


class InvitationResponse(BaseModel):
    id: str
    email: str
    role: str
    status: str
    invited_by: str
    expires_at: Optional[str] = None
    created_at: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _org_response(org: dict) -> OrgResponse:
    org_id = org["_id"]
    user_count = users_collection.count_documents({"org_id": org_id})
    # Mask stale counters: org's stored service_usage holds last month's data
    # until any user in the org triggers a new run. See is_usage_period_stale.
    raw_usage = org.get("service_usage") or {}
    if is_usage_period_stale(org.get("usage_period_start")):
        service_usage = {s: 0 for s in ALL_SERVICES}
    else:
        service_usage = {s: int(raw_usage.get(s, 0)) for s in ALL_SERVICES}
    return OrgResponse(
        id=str(org_id),
        name=org.get("name", ""),
        slug=org.get("slug", ""),
        plan=org.get("plan", "starter"),
        status=org.get("status", "active"),
        allowed_services=org.get("allowed_services", []),
        service_limits=org.get("service_limits", {}),
        service_usage=service_usage,
        allowed_domains=org.get("allowed_domains", []),
        created_at=org["created_at"].isoformat() if org.get("created_at") else None,
        user_count=user_count,
    )


def _inv_response(inv: dict) -> InvitationResponse:
    return InvitationResponse(
        id=str(inv["_id"]),
        email=inv.get("email", ""),
        role=inv.get("role", "member"),
        status=inv.get("status", "pending"),
        invited_by=inv.get("invited_by", ""),
        expires_at=inv["expires_at"].isoformat() if inv.get("expires_at") else None,
        created_at=inv["created_at"].isoformat() if inv.get("created_at") else None,
    )


# ── Organization CRUD ──────────────────────────────────────────────────────────

@router.post("/orgs", response_model=OrgResponse, status_code=201)
async def create_organization(
    body: CreateOrgRequest,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: create a new tenant organization."""
    _require_platform_admin(current_user)

    invalid_services = set(body.allowed_services) - VALID_SERVICES
    if invalid_services:
        raise HTTPException(400, f"Unknown services: {sorted(invalid_services)}")
    if body.plan not in VALID_PLANS:
        raise HTTPException(400, f"Invalid plan. Choose from: {sorted(VALID_PLANS)}")
    for svc, val in body.service_limits.items():
        if svc not in VALID_SERVICES:
            raise HTTPException(400, f"Unknown service in limits: {svc}")
        if not isinstance(val, int) or val < 0:
            raise HTTPException(400, f"Limit for {svc} must be a non-negative integer")

    # Fill any missing service limits with defaults
    limits = {s: DEFAULT_ORG_SERVICE_LIMIT for s in ALL_SERVICES}
    limits.update(body.service_limits)

    org = create_org(
        name=body.name,
        allowed_services=body.allowed_services,
        service_limits=limits,
        allowed_domains=body.allowed_domains,
        plan=body.plan,
        created_by=current_user["email"],
    )
    logger.info("Org created: %s by %s", org["name"], current_user["email"])
    return _org_response(org)


@router.get("/orgs", response_model=List[OrgResponse])
async def list_organizations(
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: list all tenant organizations."""
    _require_platform_admin(current_user)
    return [_org_response(o) for o in list_orgs(skip=skip, limit=limit)]


@router.get("/orgs/{org_id}", response_model=OrgResponse)
async def get_organization(
    org_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: get a single org."""
    _require_platform_admin(current_user)
    org = get_org_by_id(org_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    return _org_response(org)


@router.patch("/orgs/{org_id}", response_model=OrgResponse)
async def update_organization(
    org_id: str,
    body: UpdateOrgRequest,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: update org settings (limits, services, domains, status)."""
    _require_platform_admin(current_user)

    org = get_org_by_id(org_id)
    if not org:
        raise HTTPException(404, "Organization not found")

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.plan is not None:
        if body.plan not in VALID_PLANS:
            raise HTTPException(400, f"Invalid plan. Choose from: {sorted(VALID_PLANS)}")
        updates["plan"] = body.plan
    if body.allowed_services is not None:
        invalid = set(body.allowed_services) - VALID_SERVICES
        if invalid:
            raise HTTPException(400, f"Unknown services: {sorted(invalid)}")
        updates["allowed_services"] = body.allowed_services
    if body.service_limits is not None:
        for svc, val in body.service_limits.items():
            if svc not in VALID_SERVICES:
                raise HTTPException(400, f"Unknown service: {svc}")
            if not isinstance(val, int) or val < 0:
                raise HTTPException(400, f"Limit for {svc} must be a non-negative integer")
        updates["service_limits"] = body.service_limits
    if body.allowed_domains is not None:
        updates["allowed_domains"] = [d.lower().strip() for d in body.allowed_domains]
    if body.status is not None:
        if body.status not in ("active", "suspended"):
            raise HTTPException(400, "status must be 'active' or 'suspended'")
        updates["status"] = body.status

    if not updates:
        raise HTTPException(400, "No fields to update")

    update_org(org_id, updates)
    logger.info("Org %s updated by %s: %s", org_id, current_user["email"], list(updates.keys()))
    return _org_response(get_org_by_id(org_id))


# ── Invitations ────────────────────────────────────────────────────────────────

@router.post("/orgs/{org_id}/invitations", status_code=201)
async def invite_user_to_org(
    org_id: str,
    body: InviteUserRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Platform admin: invite a specific email to an org.
    The invited user will be able to sign in with Google (or email/password)
    and will automatically be placed in this org.

    Returns the invitation details including the raw token (shown once — send it to the user).
    In production, trigger an email here instead.
    """
    _require_platform_admin(current_user)

    org = get_org_by_id(org_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    if body.role not in ("member", "admin"):
        raise HTTPException(400, "role must be 'member' or 'admin'")

    email = body.email.lower().strip()

    # Check if already a user
    existing = users_collection.find_one({"email": email})
    if existing:
        raise HTTPException(400, f"{email} is already a user on AdLens")

    invite, raw_token = create_invitation(
        org_id=org["_id"],
        email=email,
        role=body.role,
        invited_by=current_user["email"],
        expires_hours=body.expires_hours,
    )
    logger.info("Invitation created for %s to org %s", email, org["name"])

    # Send invitation email (non-blocking — failure doesn't break the API)
    inviter_name = current_user.get("display_name") or current_user.get("email", "AdLens Admin")
    send_invitation_email(
        to_email=email,
        org_name=org["name"],
        inviter_name=inviter_name,
        role=body.role,
    )

    return {
        "detail": f"Invitation created for {email}",
        "invitation_id": str(invite["_id"]),
        "email": email,
        "org": org["name"],
        "role": body.role,
        "expires_hours": body.expires_hours,
        # In production: send raw_token via email instead of returning it in the response
        "invite_token": raw_token,
    }


@router.get("/orgs/{org_id}/invitations", response_model=List[InvitationResponse])
async def list_invitations(
    org_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: list all invitations for an org."""
    _require_platform_admin(current_user)
    if not get_org_by_id(org_id):
        raise HTTPException(404, "Organization not found")
    return [_inv_response(i) for i in list_org_invitations(org_id)]


# ── Org Users ──────────────────────────────────────────────────────────────────

@router.get("/orgs/{org_id}/users")
async def list_org_users(
    org_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: list all users in an org."""
    _require_platform_admin(current_user)
    if not get_org_by_id(org_id):
        raise HTTPException(404, "Organization not found")

    users = list(users_collection.find({"org_id": ObjectId(org_id)}).sort("email", 1))
    out = []
    for u in users:
        stale = is_usage_period_stale(u.get("usage_period_start"))
        service_usage = {s: 0 for s in ALL_SERVICES} if stale else (u.get("service_usage") or {})
        runs_this_period = 0 if stale else int(u.get("runs_this_period") or 0)
        out.append({
            "id": str(u["_id"]),
            "email": u.get("email"),
            "display_name": u.get("display_name", ""),
            "picture": u.get("picture", ""),
            "org_role": u.get("org_role", "member"),
            "auth_providers": u.get("auth_providers", ["password"]),
            "status": u.get("status", "active"),
            "service_usage": service_usage,
            "service_limits": u.get("service_limits", {}),
            "runs_this_period": runs_this_period,
            "last_login_at": u["last_login_at"].isoformat() if u.get("last_login_at") else None,
            "joined_at": u["created_at"].isoformat() if u.get("created_at") else None,
        })
    return out


@router.get("/orgs/{org_id}/usage-history")
async def get_organization_usage_history(
    org_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: return month-by-month usage history for an org (newest first, up to 24 months)."""
    _require_platform_admin(current_user)
    if not get_org_by_id(org_id):
        raise HTTPException(404, "Organization not found")
    return get_org_usage_history(org_id)


@router.patch("/orgs/{org_id}/users/{user_id}/status")
async def update_org_user_status(
    org_id: str,
    user_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """Platform admin: suspend or reactivate a user within an org."""
    _require_platform_admin(current_user)
    new_status = body.get("status")
    if new_status not in ("active", "suspended"):
        raise HTTPException(400, "status must be 'active' or 'suspended'")
    if not ObjectId.is_valid(user_id):
        raise HTTPException(404, "User not found")

    result = users_collection.update_one(
        {"_id": ObjectId(user_id), "org_id": ObjectId(org_id)},
        {"$set": {"status": new_status}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "User not found in this org")
    return {"detail": f"User status set to {new_status}"}


# ── Cross-org job viewer (platform-admin only) ────────────────────────────────

JOB_SERVICES = {"abcd", "studio", "resize"}


def _audit(
    admin_email: str,
    action: str,
    *,
    service: Optional[str] = None,
    job_id: Optional[str] = None,
    target_user_email: Optional[str] = None,
    target_org_id: Optional[str] = None,
) -> None:
    """Write a row to admin_audit_log. Silent — never raised, never visible to agencies."""
    try:
        admin_audit_collection.insert_one({
            "admin_email": admin_email,
            "action": action,
            "service": service,
            "job_id": job_id,
            "target_user_email": target_user_email,
            "target_org_id": str(target_org_id) if target_org_id else None,
            "at": datetime.now(timezone.utc),
        })
    except Exception:
        # Audit failures must not break the read.
        logger.exception("Failed to write admin audit row")


def _emails_for_org(org_id: ObjectId) -> List[str]:
    """All user emails currently assigned to the org. Empty list if the org has no users."""
    cur = users_collection.find({"org_id": org_id}, {"email": 1})
    return [u["email"] for u in cur if u.get("email")]


def _serialize_job_response(resp) -> Dict[str, Any]:
    """JobResponse pydantic model → JSON-friendly dict."""
    if resp is None:
        return {}
    return resp.model_dump(mode="json")


@router.get("/orgs/{org_id}/jobs")
async def list_org_jobs(
    org_id: str,
    service: Optional[str] = Query(None, description="abcd | studio | resize. If omitted, returns all three combined."),
    user_id: Optional[str] = Query(None, description="Restrict to one user in the org."),
    status_filter: Optional[str] = Query(None, alias="status", description="pending|running|processing|completed|failed"),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """
    Platform admin: list jobs across all users in an org.

    Returned items carry a `service` discriminator so the UI can render a unified
    table. When `service` is set, only that collection is queried; otherwise all
    three are merged and re-sorted by created_at.
    """
    _require_platform_admin(current_user)
    if service is not None and service not in JOB_SERVICES:
        raise HTTPException(400, f"service must be one of {sorted(JOB_SERVICES)}")
    if not ObjectId.is_valid(org_id):
        raise HTTPException(404, "Organization not found")
    org = get_org_by_id(org_id)
    if not org:
        raise HTTPException(404, "Organization not found")

    # Resolve the target email set
    if user_id is not None:
        if not ObjectId.is_valid(user_id):
            raise HTTPException(404, "User not found")
        u = users_collection.find_one(
            {"_id": ObjectId(user_id), "org_id": ObjectId(org_id)},
            {"email": 1},
        )
        if not u or not u.get("email"):
            raise HTTPException(404, "User not found in this org")
        emails = [u["email"]]
    else:
        emails = _emails_for_org(ObjectId(org_id))

    if not emails:
        return {"jobs": [], "total": 0}

    services_to_query = [service] if service else list(JOB_SERVICES)
    combined: List[Dict[str, Any]] = []

    if "abcd" in services_to_query:
        for j in list_abcd_jobs_admin(emails, status=status_filter, limit=limit, skip=skip):
            combined.append({**j, "service": "abcd"})
    if "studio" in services_to_query:
        for j in list_image_jobs_admin(emails, status=status_filter, limit=limit, skip=skip):
            combined.append({**j, "service": "studio"})
    if "resize" in services_to_query:
        for j in list_resize_jobs_admin(emails, status=status_filter, limit=limit, skip=skip):
            combined.append({**j, "service": "resize"})

    # Stable ordering across services
    combined.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    if not service:
        combined = combined[:limit]

    _audit(
        admin_email=current_user.get("email", ""),
        action="list_jobs",
        service=service,
        target_org_id=org["_id"],
    )
    return {"jobs": combined, "total": len(combined)}


@router.get("/jobs/{service}/{job_id}")
async def get_job_admin_detail(
    service: str,
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Platform admin: full detail for a single job in any service.

    Returns the same shape the user themselves would see for that service, plus
    `user_email` and `service` so the UI can render context.
    """
    _require_platform_admin(current_user)
    if service not in JOB_SERVICES:
        raise HTTPException(400, f"service must be one of {sorted(JOB_SERVICES)}")

    payload: Dict[str, Any]
    owner_email: Optional[str]

    if service == "abcd":
        resp = get_abcd_job_admin(job_id)
        if not resp:
            raise HTTPException(404, "Job not found")
        owner_email = get_abcd_job_owner(job_id)
        payload = _serialize_job_response(resp)
    elif service == "studio":
        doc = get_image_job_admin(job_id)
        if not doc:
            raise HTTPException(404, "Job not found")
        owner_email = doc.get("user_email")
        payload = {
            "job_id": doc["job_id"],
            "status": doc["status"],
            "created_at": doc["created_at"],
            "completed_at": doc.get("completed_at"),
            "prompt": doc.get("prompt"),
            "original_filename": doc.get("original_filename"),
            "original_url": doc.get("original_url"),
            "result_urls": doc.get("result_urls") or [],
            "error": doc.get("error"),
        }
    else:  # resize
        doc = get_resize_job_admin(job_id)
        if not doc:
            raise HTTPException(404, "Job not found")
        owner_email = doc.get("user_email")
        payload = {
            "job_id": doc["job_id"],
            "status": doc["status"],
            "created_at": doc["created_at"],
            "completed_at": doc.get("completed_at"),
            "original_filename": doc.get("original_filename"),
            "original_url": doc.get("original_url"),
            "sizes": doc.get("sizes") or [],
            "max_size_kb": doc.get("max_size_kb"),
            "result_urls": doc.get("result_urls") or [],
            "result_images": doc.get("result_images") or [],
            "error": doc.get("error"),
        }

    # Enrich with org context for the UI
    target_org_id = None
    if owner_email:
        u = users_collection.find_one({"email": owner_email}, {"org_id": 1, "display_name": 1})
        if u:
            target_org_id = u.get("org_id")
            payload["user_display_name"] = u.get("display_name") or ""

    payload["service"] = service
    payload["user_email"] = owner_email

    _audit(
        admin_email=current_user.get("email", ""),
        action="view_job",
        service=service,
        job_id=job_id,
        target_user_email=owner_email,
        target_org_id=target_org_id,
    )
    return payload

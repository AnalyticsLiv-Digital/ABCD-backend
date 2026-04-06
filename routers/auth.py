from datetime import datetime, timedelta, timezone
import logging
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from auth_utils import create_access_token, decode_access_token
from user_repository import create_user, get_user_by_email, list_users, update_user_services, verify_password
from db import access_requests_collection


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/auth", tags=["auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserBase(BaseModel):
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=72)
    max_runs_per_month: int = 20


VALID_SERVICES = {"abcd_analyzer", "creative_studio"}


class UserPublic(UserBase):
    id: str
    plan: str
    max_runs_per_month: int
    runs_this_period: int
    is_admin: bool
    allowed_services: List[str] = ["abcd_analyzer"]


class UserAdminView(UserPublic):
    """Extended user view for admin panel."""
    pass


class UpdateServicesRequest(BaseModel):
    allowed_services: List[str]


class AccessRequestIn(BaseModel):
    email: EmailStr
    message: str = Field("", max_length=1000)


class AccessRequestOut(BaseModel):
    id: str
    email: EmailStr
    message: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class AccessDecision(BaseModel):
    max_runs_per_month: int = 20
    note: str = Field("", max_length=1000)


def _user_public(user: dict) -> UserPublic:
    roles = user.get("roles") or []
    is_admin_user = "admin" in roles
    # Default: admin gets all services; regular users get abcd_analyzer only.
    default_services = ["abcd_analyzer", "creative_studio"] if is_admin_user else ["abcd_analyzer"]
    return UserPublic(
        id=str(user["_id"]),
        email=user["email"],
        plan=user.get("plan", "beta"),
        max_runs_per_month=int(user.get("max_runs_per_month") or 0),
        runs_this_period=int(user.get("runs_this_period") or 0),
        is_admin=is_admin_user,
        allowed_services=user.get("allowed_services", default_services),
    )


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    email = decode_access_token(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = get_user_by_email(email)  # we use email as subject
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


@router.post("/register", response_model=UserPublic)
async def register(user_in: UserCreate):
    """Create a user. For production, restrict this endpoint (e.g. admin-only or invite-only)."""
    try:
        user = create_user(
            email=user_in.email,
            password=user_in.password,
            max_runs_per_month=user_in.max_runs_per_month,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _user_public(user)


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = get_user_by_email(form_data.username)
    if not user or not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    access_token = create_access_token(subject=user["email"])
    return Token(access_token=access_token)


@router.get("/me", response_model=UserPublic)
async def me(current_user: dict = Depends(get_current_user)):
    """Return current user's profile and usage."""
    return _user_public(current_user)


@router.post("/request-access", status_code=202)
async def request_access(payload: AccessRequestIn):
    """Store an access request; for now we log it and store in Mongo."""
    now = datetime.now(timezone.utc)
    doc = {
        "email": payload.email.lower().strip(),
        "message": payload.message,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    access_requests_collection.insert_one(doc)
    logger.info("Access request from %s: %s", payload.email, payload.message)
    return {"detail": "Request received. We will contact you if access is granted."}


def _ensure_admin(current_user: dict) -> None:
    roles = current_user.get("roles") or []
    if "admin" not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


@router.get("/access-requests", response_model=list[AccessRequestOut])
async def list_access_requests(
    status_filter: str = "pending",
    current_user: dict = Depends(get_current_user),
):
    """Admin: list access requests (default pending only)."""
    _ensure_admin(current_user)
    query = {}
    if status_filter != "all":
        query["status"] = status_filter
    docs = list(access_requests_collection.find(query).sort("created_at", -1))
    out: list[AccessRequestOut] = []
    for d in docs:
        out.append(
            AccessRequestOut(
                id=str(d["_id"]),
                email=d["email"],
                message=d.get("message") or "",
                status=d.get("status", "pending"),
                created_at=(d.get("created_at") or "").isoformat() if d.get("created_at") else None,
                updated_at=(d.get("updated_at") or "").isoformat() if d.get("updated_at") else None,
            )
        )
    return out


@router.post("/access-requests/{request_id}/approve")
async def approve_access_request(
    request_id: str,
    decision: AccessDecision,
    current_user: dict = Depends(get_current_user),
):
    """Admin: approve an access request and (optionally) create a user."""
    _ensure_admin(current_user)
    if not ObjectId.is_valid(request_id):
        raise HTTPException(status_code=404, detail="Request not found")
    req = access_requests_collection.find_one({"_id": ObjectId(request_id)})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.get("status") == "approved":
        raise HTTPException(status_code=400, detail="Request already approved")
    email = req["email"]

    # If user already exists, just mark approved
    user = get_user_by_email(email)
    generated_password: str | None = None
    if not user:
        # Generate a simple password for now; admin can share it manually
        generated_password = f"Abcd-{ObjectId().binary.hex()[:8]}"
        user = create_user(email=email, password=generated_password, max_runs_per_month=decision.max_runs_per_month)

    now = datetime.now(timezone.utc)
    access_requests_collection.update_one(
        {"_id": req["_id"]},
        {
            "$set": {
                "status": "approved",
                "updated_at": now,
                "approved_by": current_user["email"],
                "note": decision.note,
            }
        },
    )
    return {
        "detail": "Access approved",
        "email": email,
        "generated_password": generated_password,
        "max_runs_per_month": decision.max_runs_per_month,
    }


@router.post("/access-requests/{request_id}/reject")
async def reject_access_request(
    request_id: str,
    decision: AccessDecision,
    current_user: dict = Depends(get_current_user),
):
    """Admin: reject an access request (does not create a user)."""
    _ensure_admin(current_user)
    if not ObjectId.is_valid(request_id):
        raise HTTPException(status_code=404, detail="Request not found")
    req = access_requests_collection.find_one({"_id": ObjectId(request_id)})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    now = datetime.now(timezone.utc)
    access_requests_collection.update_one(
        {"_id": req["_id"]},
        {
            "$set": {
                "status": "rejected",
                "updated_at": now,
                "rejected_by": current_user["email"],
                "note": decision.note,
            }
        },
    )
    return {"detail": "Access request rejected"}


# ── User management (admin only) ─────────────────────────────────────────────

@router.get("/users", response_model=list[UserAdminView])
async def list_all_users(
    skip: int = 0,
    limit: int = 200,
    current_user: dict = Depends(get_current_user),
):
    """Admin: list all users with their service permissions."""
    _ensure_admin(current_user)
    docs = list_users(skip=skip, limit=limit)
    return [_user_public(d) for d in docs]


@router.patch("/users/{user_id}/services")
async def update_user_service_access(
    user_id: str,
    body: UpdateServicesRequest,
    current_user: dict = Depends(get_current_user),
):
    """Admin: update which services a user can access."""
    _ensure_admin(current_user)
    invalid = set(body.allowed_services) - VALID_SERVICES
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown services: {sorted(invalid)}")
    success = update_user_services(user_id, body.allowed_services)
    if not success:
        raise HTTPException(status_code=404, detail="User not found")
    return {"detail": "Services updated", "allowed_services": body.allowed_services}


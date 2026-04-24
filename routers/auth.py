from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from auth_utils import create_access_token, decode_access_token, verify_google_token
from email_service import send_welcome_email
from user_repository import (
    ALL_SERVICES,
    DEFAULT_SERVICE_LIMIT,
    create_user,
    get_user_by_email,
    list_users,
    update_user_services,
    update_user_service_limits,
    verify_password,
)
from org_repository import (
    get_org_by_id,
    resolve_org_for_google_user,
    accept_invitation,
    get_pending_invite_for_email,
)
from db import access_requests_collection, users_collection


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


VALID_SERVICES = {"abcd_analyzer", "creative_studio", "creative_resize"}


class UserPublic(UserBase):
    id: str
    plan: str
    max_runs_per_month: int
    runs_this_period: int
    is_admin: bool
    is_platform_admin: bool = False
    org_id: Optional[str] = None
    org_role: Optional[str] = None
    display_name: Optional[str] = None
    picture: Optional[str] = None
    auth_providers: List[str] = ["password"]
    allowed_services: List[str] = ["abcd_analyzer"]
    service_limits: Dict[str, int] = {}
    service_usage: Dict[str, int] = {}


class UserAdminView(UserPublic):
    """Extended user view for admin panel."""
    pass


class UpdateServicesRequest(BaseModel):
    allowed_services: List[str]


class UpdateLimitsRequest(BaseModel):
    service_limits: Dict[str, int]


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


class GoogleLoginBody(BaseModel):
    """Body for POST /auth/google — credential is the ID token from Google GSI."""
    credential: str  # Google JWT id_token


def _user_public(user: dict) -> UserPublic:
    roles = user.get("roles") or []
    is_admin_user = "admin" in roles
    default_services = ["abcd_analyzer", "creative_studio"] if is_admin_user else ["abcd_analyzer"]

    # Back-fill service_limits for users created before this feature
    raw_limits = user.get("service_limits") or {}
    service_limits = {s: int(raw_limits.get(s, DEFAULT_SERVICE_LIMIT)) for s in ALL_SERVICES}

    raw_usage = user.get("service_usage") or {}
    service_usage = {s: int(raw_usage.get(s, 0)) for s in ALL_SERVICES}

    org_id = user.get("org_id")
    return UserPublic(
        id=str(user["_id"]),
        email=user["email"],
        plan=user.get("plan") or "beta",
        max_runs_per_month=int(user.get("max_runs_per_month") or 0),
        runs_this_period=int(user.get("runs_this_period") or 0),
        is_admin=is_admin_user,
        is_platform_admin=bool(user.get("is_platform_admin")),
        org_id=str(org_id) if org_id else None,
        org_role=user.get("org_role"),
        display_name=user.get("display_name"),
        picture=user.get("picture"),
        auth_providers=user.get("auth_providers", ["password"]),
        allowed_services=user.get("allowed_services", default_services),
        service_limits=service_limits,
        service_usage=service_usage,
    )


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    email = decode_access_token(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.get("status") == "suspended":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended. Contact your administrator.")

    # Attach org context so routes can read it without a second DB call
    org_id = user.get("org_id")
    if org_id:
        user["_org"] = get_org_by_id(org_id)
    else:
        user["_org"] = None

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
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if not user.get("password_hash"):
        # OAuth-only user — no password set, must use Google Sign-In
        raise HTTPException(
            status_code=400,
            detail="This account uses Google Sign-In. Please click 'Sign in with Google'."
        )
    if not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if user.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended. Contact your administrator.")
    access_token = create_access_token(subject=user["email"])
    return Token(access_token=access_token)


@router.post("/google", response_model=Token)
async def google_login(body: GoogleLoginBody):
    """
    Sign in with Google.

    Flow:
      1. Frontend gets a credential (id_token) from Google Sign-In (GSI)
      2. POST that token here
      3. We verify it with Google's servers
      4. We check if the user's email is invited or their domain is whitelisted to an org
      5. We create the user if first-time, then issue an AdLens JWT

    Returns 403 if the user's email/domain is not linked to any organization.
    """
    try:
        idinfo = verify_google_token(body.credential)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    email = idinfo["email"].lower().strip()
    google_sub = idinfo["sub"]  # stable Google user ID
    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")

    # --- Find or create the user ---
    user = get_user_by_email(email)

    if user is None:
        # New user — check if they're allowed in (invite or domain whitelist)
        org, org_role = resolve_org_for_google_user(email)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Your account is not linked to any organization on AdLens. "
                    "Ask your administrator to invite you or whitelist your email domain."
                ),
            )

        # Create the user record (no password for OAuth users)
        now = datetime.now(timezone.utc)
        doc = {
            "email": email,
            "password_hash": None,           # OAuth-only user
            "auth_providers": ["google"],
            "google_sub": google_sub,
            "display_name": name,
            "picture": picture,
            "roles": ["user"],
            "is_platform_admin": False,
            "org_id": org["_id"],
            "org_role": org_role,
            "status": "active",
            "plan": org.get("plan", "starter"),
            # Keep legacy fields so existing _user_public() still works
            "allowed_services": org.get("allowed_services", ["abcd_analyzer"]),
            "service_limits": org.get("service_limits", {}),
            "service_usage": {s: 0 for s in ALL_SERVICES},
            "max_runs_per_month": 0,
            "runs_this_period": 0,
            "usage_period_start": now,
            "created_at": now,
            "last_login_at": now,
        }
        result = users_collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        user = doc

        # Mark invitation as accepted if one existed
        invite = get_pending_invite_for_email(email)
        if invite:
            accept_invitation(invite["_id"])

        logger.info("New Google user created: %s (org=%s)", email, org.get("name"))

        # Send welcome email to new user (non-blocking)
        send_welcome_email(
            to_email=email,
            display_name=name,
            org_name=org.get("name", "AdLens"),
            allowed_services=org.get("allowed_services", ["abcd_analyzer"]),
        )

    else:
        # Existing user — update login timestamp and Google sub (in case of first Google login)
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "last_login_at": datetime.now(timezone.utc),
                "google_sub": google_sub,
                "display_name": name,
                "picture": picture,
            }, "$addToSet": {"auth_providers": "google"}},
        )

    if user.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended. Contact your administrator.")

    access_token = create_access_token(subject=user["email"])
    return Token(access_token=access_token)


@router.get("/me", response_model=UserPublic)
async def me(current_user: dict = Depends(get_current_user)):
    """Return current user's profile and usage."""
    return _user_public(current_user)


@router.post("/refresh", response_model=Token)
async def refresh_token(current_user: dict = Depends(get_current_user)):
    """Issue a fresh token for an existing valid session. Call on app startup to extend the session."""
    access_token = create_access_token(subject=current_user["email"])
    return Token(access_token=access_token)


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


@router.patch("/users/{user_id}/limits")
async def update_user_service_limits_endpoint(
    user_id: str,
    body: UpdateLimitsRequest,
    current_user: dict = Depends(get_current_user),
):
    """Admin: set per-module monthly usage limits for a user."""
    _ensure_admin(current_user)
    invalid = set(body.service_limits.keys()) - VALID_SERVICES
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown services: {sorted(invalid)}")
    for svc, val in body.service_limits.items():
        if not isinstance(val, int) or val < 0:
            raise HTTPException(status_code=400, detail=f"Limit for {svc} must be a non-negative integer")
    success = update_user_service_limits(user_id, body.service_limits)
    if not success:
        raise HTTPException(status_code=404, detail="User not found")
    return {"detail": "Limits updated", "service_limits": body.service_limits}


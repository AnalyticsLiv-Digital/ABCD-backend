from datetime import datetime, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from passlib.context import CryptContext

from config import settings
from db import users_collection


# Use PBKDF2-SHA256 to avoid bcrypt backend issues on Windows
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# Services tracked for per-module usage
ALL_SERVICES = ["abcd_analyzer", "creative_studio", "creative_resize"]
DEFAULT_SERVICE_LIMIT = 20


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def is_usage_period_stale(period_start) -> bool:
    """
    True if a stored usage counter belongs to a previous calendar month.

    Counter resets are lazy — they only happen when the entity (user or org)
    next attempts a run. For inactive users/orgs, the stored counter still
    reflects last month's data. Display-layer code must mask those values
    so totals are consistent across users/orgs/period boundaries.
    """
    if not period_start:
        return False
    if isinstance(period_start, str):
        try:
            period_start = datetime.fromisoformat(period_start)
        except ValueError:
            return False
    now = datetime.now(timezone.utc)
    return period_start.year != now.year or period_start.month != now.month


def get_user_by_email(email: str) -> Optional[dict]:
    return users_collection.find_one({"email": email.lower().strip()})


def get_user_by_id(user_id: str) -> Optional[dict]:
    return users_collection.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None


def create_user(email: str, password: str, max_runs_per_month: int = 20) -> dict:
    email_norm = email.lower().strip()
    if get_user_by_email(email_norm):
        raise ValueError("User with this email already exists")
    now = datetime.now(timezone.utc)
    is_first_user = users_collection.count_documents({}) == 0
    default_services = ["abcd_analyzer", "creative_studio"] if is_first_user else ["abcd_analyzer"]
    doc = {
        "email": email_norm,
        "password_hash": hash_password(password),
        "roles": ["admin"] if is_first_user else ["user"],
        "plan": "beta",
        "max_runs_per_month": max_runs_per_month,
        "runs_this_period": 0,
        "usage_period_start": now,
        "allowed_services": default_services,
        # Per-module usage limits (admin-configurable, default 20 each)
        "service_limits": {s: DEFAULT_SERVICE_LIMIT for s in ALL_SERVICES},
        # Per-module usage counters (reset each calendar month)
        "service_usage": {s: 0 for s in ALL_SERVICES},
    }
    result = users_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def update_user_services(user_id: str, allowed_services: List[str]) -> bool:
    """Update which services a user can access. Returns True if user was found."""
    if not ObjectId.is_valid(user_id):
        return False
    result = users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"allowed_services": allowed_services}},
    )
    return result.matched_count > 0


def update_user_service_limits(user_id: str, service_limits: Dict[str, int]) -> bool:
    """Admin: set per-module usage limits for a user. Returns True if user was found."""
    if not ObjectId.is_valid(user_id):
        return False
    result = users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"service_limits": service_limits}},
    )
    return result.matched_count > 0


def list_users(skip: int = 0, limit: int = 200) -> List[dict]:
    """List all users sorted by email, for admin use."""
    return list(users_collection.find({}, sort=[("email", 1)]).skip(skip).limit(limit))


def check_and_increment_service_usage(user: dict, service_id: str) -> bool:
    """
    Check per-module usage limit for a user and increment if allowed.

    Rules:
    - Admin users: always allowed (infinite usage, no increment stored).
    - Monthly reset: if the current calendar month differs from usage_period_start,
      all service_usage counters reset to 0 before checking.
    - Limit source: user.service_limits[service_id], defaults to DEFAULT_SERVICE_LIMIT (20).

    Returns True if the action is allowed (and counter was incremented).
    Returns False if the limit is already reached.
    """
    roles = user.get("roles") or []
    if "admin" in roles:
        return True  # admins are unlimited — don't store usage

    now = datetime.now(timezone.utc)

    # Detect new calendar month
    period_start = user.get("usage_period_start") or now
    if isinstance(period_start, str):
        period_start = datetime.fromisoformat(period_start)
    new_period = period_start.year != now.year or period_start.month != now.month

    # Current usage for this service (0 if new period or never used)
    if new_period:
        current_usage = 0
    else:
        service_usage = user.get("service_usage") or {}
        current_usage = int(service_usage.get(service_id, 0))

    # Limit for this service
    service_limits = user.get("service_limits") or {}
    limit = int(service_limits.get(service_id, DEFAULT_SERVICE_LIMIT))

    if current_usage >= limit:
        return False

    # Increment
    new_usage = current_usage + 1
    total_runs = new_usage if new_period else (int(user.get("runs_this_period") or 0) + 1)

    if new_period:
        # Reset all service counters, set only this one to 1
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "service_usage": {service_id: new_usage},
                "usage_period_start": now,
                "runs_this_period": new_usage,
            }},
        )
    else:
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$inc": {
                f"service_usage.{service_id}": 1,
                "runs_this_period": 1,
            }},
        )

    return True


def can_consume_run_and_increment(user: dict) -> bool:
    """
    Legacy global limit check — kept for backward compat.
    New code should use check_and_increment_service_usage() instead.
    """
    return check_and_increment_service_usage(user, "abcd_analyzer")


def check_usage_with_org(user: dict, service_id: str) -> tuple[bool, str]:
    """
    Full usage check: org-level cap (outer) then user-level cap (inner).
    Admins bypass both.

    Returns (allowed: bool, error_message: str).
    Increments both org and user counters atomically on success.
    Rolls back org counter if user limit is hit.
    """
    roles = user.get("roles") or []
    if "admin" in roles:
        return True, ""

    org_id = user.get("org_id")

    # Check org-level cap first (outer boundary)
    if org_id:
        from org_repository import get_org_by_id, check_and_increment_org_usage, decrement_org_usage
        org = get_org_by_id(org_id)
        if org is not None:
            if org.get("status") != "active":
                return False, "Your organization's account is suspended. Contact your admin."
            if not check_and_increment_org_usage(org, service_id):
                return False, (
                    "Your organization's monthly usage limit has been reached for this service. "
                    "Contact your admin to increase the limit."
                )
            # Org passed — now check user limit. Roll back org if user is blocked.
            if not check_and_increment_service_usage(user, service_id):
                decrement_org_usage(org_id, service_id)
                return False, (
                    "Your personal monthly usage limit has been reached. "
                    "Contact an admin to increase your limit."
                )
            return True, ""
        # org_id set but org not found — treat as no-org user (org may have been deleted)

    # No org — only user-level check
    if not check_and_increment_service_usage(user, service_id):
        return False, "Monthly usage limit reached. Contact an admin to increase your limit."

    return True, ""

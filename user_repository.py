from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from passlib.context import CryptContext

from config import settings
from db import users_collection


# Use PBKDF2-SHA256 to avoid bcrypt backend issues on Windows
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


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
    doc = {
        "email": email_norm,
        "password_hash": hash_password(password),
        "roles": ["admin"] if is_first_user else ["user"],
        "plan": "beta",
        "max_runs_per_month": max_runs_per_month,
        "runs_this_period": 0,
        "usage_period_start": now,
    }
    result = users_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def can_consume_run_and_increment(user: dict) -> bool:
    """Check usage limits for the user and increment runs if allowed.

    - If usage_period_start is in a previous calendar month, reset counters.
    - If runs_this_period < max_runs_per_month, increment and return True.
    - Else, return False.
    """
    now = datetime.now(timezone.utc)
    period_start = user.get("usage_period_start") or now
    if isinstance(period_start, str):
        period_start = datetime.fromisoformat(period_start)

    # New period if month or year changed
    new_period = period_start.year != now.year or period_start.month != now.month
    if new_period:
        runs = 0
    else:
        runs = int(user.get("runs_this_period") or 0)

    max_runs = int(user.get("max_runs_per_month") or 0)
    if max_runs <= 0:
        max_runs = 0

    if max_runs and runs >= max_runs:
        return False

    # Increment
    runs += 1
    users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "runs_this_period": runs,
                "usage_period_start": now if new_period else period_start,
            }
        },
    )
    return True


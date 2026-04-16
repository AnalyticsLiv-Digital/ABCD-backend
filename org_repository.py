"""
Organization and invitation management.

Orgs are the top-level tenant. Every external user belongs to exactly one org.
Internal platform admins (is_platform_admin=True) are not scoped to any org.

Access model:
  - Platform admin (analyticsliv team): manages all orgs, sets service limits
  - Org admin: manages users within their org (invite, remove, promote)
  - Org member: uses the services allowed for their org

External users gain access one of two ways:
  1. Domain whitelist: org.allowed_domains includes their Google email domain
  2. Specific invite: an invitation exists for their exact email
"""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from db import invitations_collection, organizations_collection, users_collection

ALL_SERVICES = ["abcd_analyzer", "creative_studio", "creative_resize"]
DEFAULT_ORG_SERVICE_LIMIT = 50  # runs/month per service for a new org


# ── Organizations ─────────────────────────────────────────────────────────────

def create_org(
    name: str,
    allowed_services: list[str] | None = None,
    service_limits: dict[str, int] | None = None,
    allowed_domains: list[str] | None = None,
    plan: str = "starter",
    created_by: str = "",
) -> dict:
    """Create a new tenant organization."""
    now = datetime.now(timezone.utc)
    slug = _slugify(name)

    # Ensure slug uniqueness
    if organizations_collection.find_one({"slug": slug}):
        slug = f"{slug}-{ObjectId()!s:.6}"

    doc = {
        "name": name,
        "slug": slug,
        "plan": plan,
        "status": "active",
        "allowed_services": allowed_services or ["abcd_analyzer"],
        "service_limits": service_limits or {s: DEFAULT_ORG_SERVICE_LIMIT for s in ALL_SERVICES},
        "service_usage": {s: 0 for s in ALL_SERVICES},
        "usage_period_start": now,
        # Domain whitelist: anyone whose Google email domain matches auto-joins this org
        "allowed_domains": [d.lower().strip() for d in (allowed_domains or [])],
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
    }
    result = organizations_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def get_org_by_id(org_id) -> Optional[dict]:
    if not ObjectId.is_valid(org_id):
        return None
    return organizations_collection.find_one({"_id": ObjectId(org_id)})


def get_org_for_domain(email_domain: str) -> Optional[dict]:
    """Find an active org that has whitelisted this email domain."""
    return organizations_collection.find_one({
        "status": "active",
        "allowed_domains": email_domain.lower().strip(),
    })


def list_orgs(skip: int = 0, limit: int = 100) -> list[dict]:
    return list(organizations_collection.find({}).sort("name", 1).skip(skip).limit(limit))


def update_org(org_id: str, updates: dict) -> bool:
    if not ObjectId.is_valid(org_id):
        return False
    updates["updated_at"] = datetime.now(timezone.utc)
    result = organizations_collection.update_one(
        {"_id": ObjectId(org_id)},
        {"$set": updates},
    )
    return result.matched_count > 0


def check_and_increment_org_usage(org: dict, service_id: str) -> bool:
    """
    Atomically check org service limit and increment if allowed.
    Returns True if the run is allowed (counter incremented).
    Returns False if the monthly limit is already reached.
    """
    now = datetime.now(timezone.utc)
    period_start = org.get("usage_period_start") or now
    if isinstance(period_start, str):
        period_start = datetime.fromisoformat(period_start)

    new_month = period_start.year != now.year or period_start.month != now.month
    limit = int((org.get("service_limits") or {}).get(service_id, DEFAULT_ORG_SERVICE_LIMIT))

    if new_month:
        # Reset period first, then try to increment
        organizations_collection.update_one(
            {"_id": org["_id"]},
            {"$set": {
                "service_usage": {s: 0 for s in ALL_SERVICES},
                "usage_period_start": now,
            }},
        )

    # Atomic: only increment if current usage < limit
    result = organizations_collection.find_one_and_update(
        {
            "_id": org["_id"],
            "status": "active",
            f"service_usage.{service_id}": {"$lt": limit},
        },
        {"$inc": {f"service_usage.{service_id}": 1}},
        return_document=True,
    )
    return result is not None


# ── Invitations ────────────────────────────────────────────────────────────────

def create_invitation(
    org_id,
    email: str,
    role: str = "member",
    invited_by: str = "",
    expires_hours: int = 72,
) -> tuple[dict, str]:
    """
    Create an invitation for a specific email address.
    Returns (invitation_doc, raw_token).
    raw_token is shown once; only the hash is stored.
    """
    from datetime import timedelta
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    now = datetime.now(timezone.utc)
    doc = {
        "org_id": ObjectId(org_id),
        "email": email.lower().strip(),
        "role": role,
        "status": "pending",
        "token_hash": token_hash,
        "invited_by": invited_by,
        "created_at": now,
        "expires_at": now + timedelta(hours=expires_hours),
    }
    result = invitations_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc, raw_token


def get_pending_invite_for_email(email: str) -> Optional[dict]:
    """Find a pending, non-expired invitation for this email."""
    return invitations_collection.find_one({
        "email": email.lower().strip(),
        "status": "pending",
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })


def accept_invitation(invite_id) -> bool:
    result = invitations_collection.update_one(
        {"_id": ObjectId(invite_id)},
        {"$set": {"status": "accepted", "accepted_at": datetime.now(timezone.utc)}},
    )
    return result.modified_count > 0


def list_org_invitations(org_id: str) -> list[dict]:
    return list(invitations_collection.find({"org_id": ObjectId(org_id)}).sort("created_at", -1))


# ── User ↔ Org linking ────────────────────────────────────────────────────────

def resolve_org_for_google_user(email: str) -> tuple[Optional[dict], str]:
    """
    Given a Google-authenticated email, find which org this user belongs to.

    Resolution order:
      1. Existing user record (already has org_id)
      2. Pending invitation for this exact email
      3. Domain whitelist on any active org

    Returns (org_doc_or_None, role)
    role is "member" by default, "admin" if the invite specifies it.
    """
    # Check existing user first
    existing = users_collection.find_one({"email": email.lower().strip()})
    if existing and existing.get("org_id"):
        org = get_org_by_id(existing["org_id"])
        return org, existing.get("org_role", "member")

    # Check invitation
    invite = get_pending_invite_for_email(email)
    if invite:
        org = get_org_by_id(invite["org_id"])
        return org, invite.get("role", "member")

    # Check domain whitelist
    domain = email.split("@")[-1]
    org = get_org_for_domain(domain)
    if org:
        return org, "member"

    return None, "member"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    import re
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")

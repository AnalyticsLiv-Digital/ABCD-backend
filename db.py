from pymongo import MongoClient, ASCENDING, DESCENDING

from config import settings


_client = MongoClient(settings.MONGODB_URI)
_db = _client[settings.MONGODB_DB_NAME]

# Collections
jobs_collection = _db[settings.MONGODB_JOBS_COLLECTION]
users_collection = _db["users"]
access_requests_collection = _db["access_requests"]
image_jobs_collection = _db["image_jobs"]
resize_jobs_collection = _db["resize_jobs"]

# Multi-tenancy collections
organizations_collection = _db["organizations"]
invitations_collection = _db["invitations"]

# Append-only audit log for platform-admin views into customer data.
# Not exposed to agencies; queried directly by the dev team for spot checks.
admin_audit_collection = _db["admin_audit_log"]


def ensure_indexes():
    """Create indexes on startup. Safe to call multiple times (idempotent)."""
    # Users
    users_collection.create_index("email", unique=True)
    users_collection.create_index("google_sub", sparse=True)
    users_collection.create_index("org_id")

    # Organizations
    organizations_collection.create_index("slug", unique=True, sparse=True)
    organizations_collection.create_index("allowed_domains")

    # Invitations
    invitations_collection.create_index("email")
    invitations_collection.create_index("org_id")
    invitations_collection.create_index("token_hash")
    invitations_collection.create_index(
        "expires_at", expireAfterSeconds=0  # TTL index: auto-delete expired invites
    )

    # Jobs — faster per-org/per-user list queries
    jobs_collection.create_index([("user_email", ASCENDING), ("created_at", DESCENDING)])
    image_jobs_collection.create_index([("user_email", ASCENDING), ("created_at", DESCENDING)])
    resize_jobs_collection.create_index([("user_email", ASCENDING), ("created_at", DESCENDING)])

    # Audit log — query by admin and by target user
    admin_audit_collection.create_index([("admin_email", ASCENDING), ("at", DESCENDING)])
    admin_audit_collection.create_index([("target_user_email", ASCENDING), ("at", DESCENDING)])


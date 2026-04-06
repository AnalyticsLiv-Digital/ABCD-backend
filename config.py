"""
Application configuration loaded from environment.
See .env.example and docs/REFERENCE.md for variable descriptions.
Loads .env from the backend directory so GCP_PROJECT_ID etc. are read.
"""
import os
from pathlib import Path
from typing import List

# Load .env from backend directory (so it works when running from backend/ or project root)
_backend_dir = Path(__file__).resolve().parent
_env_file = _backend_dir / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)


def _str_list(value: str) -> List[str]:
    if not value or not value.strip():
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


class Settings:
    """Backend settings from environment."""

    # API server
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))

    # CORS: comma-separated origins (e.g. http://localhost:5173,http://localhost:3000)
    CORS_ORIGINS: List[str] = _str_list(os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"))

    # GCP / ABCD (Phase 2)
    GCP_PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "")
    GCP_REGION: str = os.getenv("GCP_REGION", "us-central1")
    GCS_BUCKET: str = os.getenv("GCS_BUCKET", "")
    KNOWLEDGE_GRAPH_API_KEY: str = os.getenv("KNOWLEDGE_GRAPH_API_KEY", "")
    ABCD_USE_ANNOTATIONS: bool = os.getenv("ABCD_USE_ANNOTATIONS", "false").lower() in ("1", "true", "yes")
    ABCD_USE_LLMS: bool = os.getenv("ABCD_USE_LLMS", "true").lower() in ("1", "true", "yes")
    ABCD_RUN_LONG_FORM: bool = os.getenv("ABCD_RUN_LONG_FORM", "true").lower() in ("1", "true", "yes")
    ABCD_RUN_SHORTS: bool = os.getenv("ABCD_RUN_SHORTS", "true").lower() in ("1", "true", "yes")

    # MongoDB (Phase 4 – persistent jobs)
    MONGODB_URI: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    MONGODB_DB_NAME: str = os.getenv("MONGODB_DB_NAME", "abcd_saas")
    MONGODB_JOBS_COLLECTION: str = os.getenv("MONGODB_JOBS_COLLECTION", "jobs")

    # Auth / JWT
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

    # When True and GCP_PROJECT_ID is set, run real ABCD; otherwise use mock (Phase 1)
    USE_REAL_ABCD: bool = os.getenv("USE_REAL_ABCD", "true").lower() in ("1", "true", "yes")

    # Mock job delay in seconds (used when USE_REAL_ABCD is False)
    MOCK_JOB_DELAY_SECONDS: float = float(os.getenv("MOCK_JOB_DELAY_SECONDS", "2.0"))

    # Creative Studio – image enhancement via n8n
    # The n8n webhook is called server-side (background thread), so no CORS or timeout issues.
    N8N_IMAGE_WEBHOOK_URL: str = os.getenv("N8N_IMAGE_WEBHOOK_URL", "https://n8n.analyticsliv.com/webhook/image-agent")
    # GCS bucket for storing Creative Studio images. Falls back to GCS_BUCKET if not set.
    GCS_BUCKET: str = os.getenv("GCS_BUCKET", "")


settings = Settings()

"""
ABCD Detector SaaS – backend.
Run locally: from backend/ directory, run: uvicorn main:app --reload
Or from project root: uvicorn backend.main:app --reload (with PYTHONPATH=. or install as package)
"""
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from db import jobs_collection
from routers.jobs import router as jobs_router
from routers.auth import router as auth_router
from routers.public import router as public_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("abcd-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks: config validation and basic health checks."""
    # Config sanity checks
    if settings.USE_REAL_ABCD and not settings.GCP_PROJECT_ID:
        logger.warning(
            "USE_REAL_ABCD is true but GCP_PROJECT_ID is empty; real ABCD will be disabled."
        )
    # MongoDB connectivity check
    try:
        # cheap call to verify connection
        jobs_collection.estimated_document_count()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("MongoDB connectivity check failed: %s", exc)
    yield


app = FastAPI(
    title="ABCD Detector API",
    description="Create and poll video analysis jobs using ABCD Detector.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(jobs_router)
app.include_router(public_router)


@app.get("/health")
def health():
    """Liveness/readiness for local runs and future deployment."""
    from abcd_service import is_real_abcd_available

    mongo_ok = True
    try:
        jobs_collection.estimated_document_count()
    except Exception:  # pragma: no cover - defensive
        mongo_ok = False

    return {
        "status": "ok",
        "service": "abcd-detector-api",
        "mongo_ok": mongo_ok,
        "real_abcd_available": is_real_abcd_available(),
    }


@app.get("/config/status")
def config_status():
    """Helpful for debugging: confirms whether .env is loaded and real ABCD will run."""
    from abcd_service import is_real_abcd_available
    return {
        "gcp_project_id_set": bool(settings.GCP_PROJECT_ID),
        "use_real_abcd": settings.USE_REAL_ABCD,
        "real_abcd_will_run": is_real_abcd_available(),
    }

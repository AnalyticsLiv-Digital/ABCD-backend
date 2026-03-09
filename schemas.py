"""
Pydantic models for API request/response and in-memory job store.
Result shape matches future ABCD output for frontend compatibility.
Named 'schemas' to avoid import clash with abcd_original/models.py.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ----- Request/Response (API contract) -----


class AdvancedOptions(BaseModel):
    """Optional per-job overrides for ABCD engine behavior."""

    enable_llms: Optional[bool] = Field(
        default=None,
        description="Override default ABCD_USE_LLMS for this job (None = use env default).",
    )
    enable_annotations: Optional[bool] = Field(
        default=None,
        description="Override default ABCD_USE_ANNOTATIONS for this job (None = use env default).",
    )
    features_to_evaluate: Optional[List[str]] = Field(
        default=None,
        description="Explicit list of ABCD feature ids to evaluate (None = engine default/all).",
    )
    allow_public_share: Optional[bool] = Field(
        default=False,
        description="If true, backend may pre-create a share link after completion.",
    )


class CreateJobRequest(BaseModel):
    """POST /jobs body. One of youtube_url or video_url required."""

    youtube_url: Optional[str] = Field(None, description="YouTube video URL")
    video_url: Optional[str] = Field(None, description="GCS video URI (e.g. gs://bucket/path/video.mp4)")
    brand_name: Optional[str] = Field("My Brand", description="Brand name for ABCD evaluation")

    # Extended brand + campaign metadata (optional, see docs/USER_INPUT_SCHEMA.md)
    brand_variations: Optional[List[str]] = Field(
        default=None,
        description="Optional list of brand name variations/aliases.",
    )
    products: Optional[List[str]] = Field(
        default=None,
        description="Optional list of product or service names featured in the creative.",
    )
    product_categories: Optional[List[str]] = Field(
        default=None,
        description="Optional list of product or service categories.",
    )
    call_to_actions: Optional[List[str]] = Field(
        default=None,
        description="Optional list of call-to-action phrases to highlight (e.g. 'Sign up', 'Buy now').",
    )

    campaign_name: Optional[str] = Field(
        default=None,
        description="Optional campaign name for grouping reports.",
    )
    campaign_tags: Optional[List[str]] = Field(
        default=None,
        description="Optional free-form tags for grouping/filtering (e.g. 'holiday-2025').",
    )

    creative_format: Optional[Literal["long_form", "shorts", "auto"]] = Field(
        default=None,
        description='Creative format hint: "long_form", "shorts", or "auto" (use env defaults).',
    )
    objective: Optional[Literal["awareness", "consideration", "conversion", "other"]] = Field(
        default=None,
        description="Optional campaign objective for reporting/UX purposes.",
    )

    advanced: Optional[AdvancedOptions] = Field(
        default=None,
        description="Optional advanced tuning for LLM/annotations/features.",
    )

    @field_validator("youtube_url", "video_url")
    @classmethod
    def strip_whitespace(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else None

    def get_video_identifier(self) -> Optional[str]:
        """Return the video source (youtube_url or video_url) for display."""
        return self.youtube_url or self.video_url


class FeatureResult(BaseModel):
    """Single ABCD feature result (matches upstream structure)."""

    feature_id: str
    feature_name: str
    result: str  # "Excellent" | "Might Improve" | "Needs Review"
    details: Optional[str] = None


class JobResultPayload(BaseModel):
    """Result payload when job is completed. Mirrors ABCD VideoAssessment."""

    video_uri: str  # YouTube URL or gs:// URI
    brand_name: str
    overall_score_pct: Optional[float] = Field(
        None,
        description="ABCD overall score: % of features that passed (detected). 80+ Excellent, 65–80 Might Improve, <65 Needs Review.",
    )
    overall_result: Optional[str] = Field(
        None,
        description="ABCD overall result: Excellent | Might Improve | Needs Review",
    )
    long_form_abcd: List[FeatureResult] = Field(default_factory=list)
    shorts: List[FeatureResult] = Field(default_factory=list)
    result_source: Optional[Literal["mock", "abcd"]] = Field(
        default="abcd",
        description="'mock' = placeholder data; 'abcd' = real ABCD detector run",
    )


class JobResponse(BaseModel):
    """GET /jobs/{job_id} response."""

    job_id: str
    status: JobStatus
    created_at: str  # ISO8601
    completed_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[JobResultPayload] = None
    video_identifier: Optional[str] = None  # youtube_url or video_url for display


class CreateJobResponse(BaseModel):
    """POST /jobs response."""

    job_id: str
    status: JobStatus = JobStatus.PENDING
    message: str = "Job created. Use GET /jobs/{job_id} to poll status."


class JobSummary(BaseModel):
    """Single item in GET /jobs list."""

    job_id: str
    status: JobStatus
    created_at: str
    video_identifier: Optional[str] = None


class JobListResponse(BaseModel):
    """GET /jobs response."""

    jobs: List[JobSummary]
    total: int


# ----- Mock result generator (used when ABCD is disabled) -----


def _mock_result(video_uri: str, brand_name: str) -> JobResultPayload:
    """Generate mock ABCD-style result for Phase 1."""
    return JobResultPayload(
        video_uri=video_uri,
        brand_name=brand_name,
        result_source="mock",
        overall_score_pct=66.67,
        overall_result="Might Improve",
        long_form_abcd=[
            FeatureResult(feature_id="quick_pacing", feature_name="Quick Pacing", result="Excellent", details="Mock"),
            FeatureResult(feature_id="brand_visuals", feature_name="Brand Visuals (First 5 seconds)", result="Might Improve", details="Mock"),
            FeatureResult(feature_id="cta_speech", feature_name="Call To Action (Speech)", result="Needs Review", details="Mock"),
        ],
        shorts=[
            FeatureResult(feature_id="shorts_style", feature_name="Shorts Production Style", result="Excellent", details="Mock"),
        ],
    )


def get_mock_result_payload(video_uri: str, brand_name: str) -> JobResultPayload:
    return _mock_result(video_uri, brand_name)

"""
Phase 2: Run the real ABCD detector for one video (YouTube URL or GCS URI).
Builds config from env + job, runs one-video pipeline, maps result to our API shape.
"""
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from config import settings
from schemas import FeatureResult, JobResultPayload

logger = logging.getLogger(__name__)


class AbcdConfigError(Exception):
    """Configuration/parameter problems before calling external services."""


class AbcdExternalServiceError(Exception):
    """Errors from external services (GCP APIs, network, etc.)."""


class AbcdEngineError(Exception):
    """Errors inside the abcd_original engine (unexpected states)."""

# Path to abcd_original so ABCD's imports resolve
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ABCD_DIR = os.path.join(_BACKEND_DIR, "abcd_original")


def _ensure_abcd_path():
    """Prepend abcd_original to sys.path so 'import configuration' etc. load from there."""
    if _ABCD_DIR not in sys.path:
        sys.path.insert(0, _ABCD_DIR)


def _normalise_list_str(values: Optional[List[str]]) -> str:
    if not values:
        return ""
    # Join unique, non-empty strings with comma + space to keep prompts readable.
    seen = []
    for v in values:
        v = (v or "").strip()
        if v and v not in seen:
            seen.append(v)
    return ", ".join(seen)


def _build_abcd_config(
    video_uri: str,
    brand_name: str,
    *,
    brand_variations: Optional[List[str]] = None,
    products: Optional[List[str]] = None,
    product_categories: Optional[List[str]] = None,
    call_to_actions: Optional[List[str]] = None,
    creative_format: Optional[str] = None,
    advanced: Optional[Dict[str, Any]] = None,
) -> "Configuration":
    """Build ABCD Configuration from our settings and job. Requires _ensure_abcd_path() first."""
    _ensure_abcd_path()
    from configuration import Configuration
    from models import CreativeProviderType
    from features_repository import feature_configs_handler

    config = Configuration()

    # Determine creative provider type
    is_youtube = "youtube.com" in video_uri or "youtu.be" in video_uri
    creative_provider_type = CreativeProviderType.YOUTUBE.value if is_youtube else CreativeProviderType.GCS.value

    # Bucket: required by set_parameters; use env or placeholder for YouTube
    bucket_name = (settings.GCS_BUCKET or "placeholder").strip() or "placeholder"

    # Feature ids: evaluate all features by default (may be overridden by advanced options)
    all_features = feature_configs_handler.features_configs_handler.get_all_features()
    default_features_to_evaluate = [f.id for f in all_features]

    features_to_evaluate = default_features_to_evaluate
    use_annotations = settings.ABCD_USE_ANNOTATIONS and not is_youtube
    use_llms = settings.ABCD_USE_LLMS
    run_long_form = settings.ABCD_RUN_LONG_FORM
    run_shorts = settings.ABCD_RUN_SHORTS

    if advanced:
        # features_to_evaluate: explicit override if provided
        if isinstance(advanced.get("features_to_evaluate"), list) and advanced["features_to_evaluate"]:
            features_to_evaluate = [str(fid) for fid in advanced["features_to_evaluate"]]
        # enable_llms / enable_annotations override env defaults if not None
        if advanced.get("enable_llms") is not None:
            use_llms = bool(advanced["enable_llms"])
        if advanced.get("enable_annotations") is not None and not is_youtube:
            use_annotations = bool(advanced["enable_annotations"])

    # Creative format overrides long_form/shorts toggles if provided
    if creative_format == "long_form":
        run_long_form = True
        run_shorts = False
    elif creative_format == "shorts":
        run_long_form = False
        run_shorts = True

    config.set_parameters(
        project_id=settings.GCP_PROJECT_ID or "placeholder",
        project_zone=settings.GCP_REGION,
        bucket_name=bucket_name,
        knowledge_graph_api_key=settings.KNOWLEDGE_GRAPH_API_KEY,
        bigquery_dataset="",
        bigquery_table="",
        assessment_file="",
        use_annotations=use_annotations,
        use_llms=use_llms,
        extract_brand_metadata=True,
        run_long_form_abcd=run_long_form,
        run_shorts=run_shorts,
        features_to_evaluate=features_to_evaluate,
        creative_provider_type=creative_provider_type,
        verbose=False,
    )
    config.set_videos([video_uri])
    config.set_brand_details(
        brand_name=brand_name or "Brand",
        brand_variations=_normalise_list_str(brand_variations),
        products=_normalise_list_str(products),
        products_categories=_normalise_list_str(product_categories),
        call_to_actions=_normalise_list_str(call_to_actions),
    )
    return config


def _run_single_video_assessment(config: "Configuration", video_uri: str):
    """Run ABCD pipeline for one video; return VideoAssessment. Requires _ensure_abcd_path()."""
    _ensure_abcd_path()
    import models
    from creative_providers import creative_provider_registry
    from creative_providers import creative_provider_proto
    from evaluation_services import video_evaluation_service
    from helpers import generic_helpers

    try:
        creative_provider: creative_provider_proto.CreativeProviderProto = (
            creative_provider_registry.provider_factory.get_provider(config.creative_provider_type.value)
        )
        # get_creative_uris may return a generator; normalize to a list
        video_uris = list(creative_provider.get_creative_uris(config) or [])
    except Exception as exc:
        raise AbcdEngineError(f"Failed to resolve creative URIs: {exc}") from exc

    if not video_uris:
        raise AbcdConfigError("No video URIs returned from creative provider")

    video_uri = video_uris[0]

    if config.creative_provider_type == models.CreativeProviderType.GCS and "gs://" not in video_uri:
        raise ValueError(f"GCS creative provider requires gs:// URI, got {video_uri}")
    if config.creative_provider_type == models.CreativeProviderType.YOUTUBE and "youtube.com" not in video_uri and "youtu.be" not in video_uri:
        raise ValueError(f"YouTube creative provider requires YouTube URL, got {video_uri}")

    # Annotations only for GCS
    if config.use_annotations and config.creative_provider_type == models.CreativeProviderType.GCS:
        try:
            from annotations_evaluation import annotations_generation

            annotations_generation.generate_video_annotations(config, video_uri)
        except Exception as exc:
            raise AbcdExternalServiceError(f"Video annotations generation failed: {exc}") from exc

    # Trim first 5s for long-form (GCS only)
    if config.run_long_form_abcd and config.creative_provider_type == models.CreativeProviderType.GCS:
        try:
            generic_helpers.trim_video(config, video_uri)
        except Exception as exc:
            raise AbcdEngineError(f"Failed to trim video for long-form ABCD: {exc}") from exc

    long_form_abcd_evaluated_features = []
    shorts_evaluated_features = []

    if config.run_long_form_abcd:
        try:
            long_form_abcd_evaluated_features = (
                video_evaluation_service.video_evaluation_service.evaluate_features(
                    config=config,
                    video_uri=video_uri,
                    features_category=models.VideoFeatureCategory.LONG_FORM_ABCD,
                )
            )
        except Exception as exc:
            raise AbcdExternalServiceError(f"Long-form feature evaluation failed: {exc}") from exc
    if config.run_shorts:
        try:
            shorts_evaluated_features = (
                video_evaluation_service.video_evaluation_service.evaluate_features(
                    config=config,
                    video_uri=video_uri,
                    features_category=models.VideoFeatureCategory.SHORTS,
                )
            )
        except Exception as exc:
            raise AbcdExternalServiceError(f"Shorts feature evaluation failed: {exc}") from exc

    video_assessment = models.VideoAssessment(
        brand_name=config.brand_name,
        video_uri=video_uri,
        long_form_abcd_evaluated_features=long_form_abcd_evaluated_features,
        shorts_evaluated_features=shorts_evaluated_features,
        config=config,
    )

    # Cleanup local files if any (e.g. from trim)
    try:
        generic_helpers.remove_local_video_files()
    except Exception as e:
        logger.warning("Cleanup local video files: %s", e)

    return video_assessment


def _feature_result_from_abcd(feature_eval) -> FeatureResult:
    """Map ABCD FeatureEvaluation to our FeatureResult.
    Follows standard ABCD engine: per-feature is binary (passed/failed).
    - detected=True  → Excellent (feature passed)
    - detected=False → Needs Review (feature failed)
    """
    detected = getattr(feature_eval, "detected", False)
    rationale = getattr(feature_eval, "rationale", "") or ""
    feature = getattr(feature_eval, "feature", None)
    name = feature.name if feature else ""
    fid = feature.id if feature else ""

    result_label = "Excellent" if detected else "Needs Review"

    details = rationale
    if getattr(feature_eval, "evidence", None):
        details = (details + " " + (feature_eval.evidence or "")).strip()

    return FeatureResult(
        feature_id=fid,
        feature_name=name,
        result=result_label,
        details=details or None,
    )


def _overall_result_from_score(score_pct: float) -> str:
    """ABCD standard: score >= 80 Excellent, 65–80 Might Improve, < 65 Needs Review."""
    if score_pct >= 80:
        return "Excellent"
    if score_pct >= 65:
        return "Might Improve"
    return "Needs Review"


def run_abcd_analysis(
    video_uri: str,
    brand_name: str,
    *,
    brand_variations: Optional[List[str]] = None,
    products: Optional[List[str]] = None,
    product_categories: Optional[List[str]] = None,
    call_to_actions: Optional[List[str]] = None,
    creative_format: Optional[str] = None,
    advanced: Optional[Dict[str, Any]] = None,
) -> JobResultPayload:
    """
    Run ABCD detector for one video (YouTube URL or GCS URI).
    Returns our API result payload. Raises on error.
    """
    _ensure_abcd_path()
    start = time.monotonic()
    logger.info(
        "ABCD run start",
        extra={
            "event": "abcd_start",
            "video_uri": video_uri,
            "brand_name": brand_name,
            "creative_format": creative_format,
        },
    )
    config = _build_abcd_config(
        video_uri,
        brand_name,
        brand_variations=brand_variations,
        products=products,
        product_categories=product_categories,
        call_to_actions=call_to_actions,
        creative_format=creative_format,
        advanced=advanced,
    )
    try:
        assessment = _run_single_video_assessment(config, video_uri)
    except AbcdConfigError:
        # Config errors are logged and re-raised as-is for clearer job errors.
        logger.exception("ABCD config error for video %s", video_uri)
        raise
    except AbcdExternalServiceError:
        logger.exception("ABCD external service error for video %s", video_uri)
        raise
    except AbcdEngineError:
        logger.exception("ABCD engine error for video %s", video_uri)
        raise
    except Exception as exc:
        logger.exception("ABCD unexpected error for video %s", video_uri)
        raise AbcdEngineError(f"Unexpected error in ABCD engine: {exc}") from exc

    all_evaluated = list(assessment.long_form_abcd_evaluated_features) + list(
        assessment.shorts_evaluated_features
    )
    long_form = [
        _feature_result_from_abcd(fe) for fe in assessment.long_form_abcd_evaluated_features
    ]
    shorts = [
        _feature_result_from_abcd(fe) for fe in assessment.shorts_evaluated_features
    ]

    # Overall score (ABCD standard): % of features detected, then Excellent / Might Improve / Needs Review
    total = len(all_evaluated)
    passed = sum(1 for fe in all_evaluated if getattr(fe, "detected", False))
    score_pct = (passed * 100.0 / total) if total else 0.0
    overall_result = _overall_result_from_score(score_pct)

    duration_sec = time.monotonic() - start
    logger.info(
        "ABCD run complete",
        extra={
            "event": "abcd_complete",
            "video_uri": assessment.video_uri,
            "brand_name": assessment.brand_name,
            "overall_score_pct": round(score_pct, 2),
            "overall_result": overall_result,
            "duration_sec": round(duration_sec, 2),
            "total_features": total,
            "passed_features": passed,
        },
    )

    return JobResultPayload(
        video_uri=assessment.video_uri,
        brand_name=assessment.brand_name,
        result_source="abcd",
        overall_score_pct=round(score_pct, 2),
        overall_result=overall_result,
        long_form_abcd=long_form,
        shorts=shorts,
    )


def is_real_abcd_available() -> bool:
    """True if GCP is configured and we should run real ABCD."""
    return bool(settings.GCP_PROJECT_ID and settings.USE_REAL_ABCD)

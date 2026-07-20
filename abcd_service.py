#NEW CODE BELOW 
"""Run the bundled real ABCD detector and map results to the API shape."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from google.auth.exceptions import TransportError

from config import settings
from schemas import AbcdPillarScore, FeatureResult, JobResultPayload

T = TypeVar("T")
logger = logging.getLogger(__name__)


class AbcdConfigError(Exception):
    """Configuration/parameter problems before calling external services."""


class AbcdExternalServiceError(Exception):
    """Errors from Google Cloud services, credentials, or the network."""


class AbcdEngineError(Exception):
    """Unexpected errors inside the bundled ABCD detector."""


_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ABCD_DIR = os.path.join(_BACKEND_DIR, "abcd_original")


def get_abcd_source_dir() -> str:
    """Return the absolute bundled detector path used by this backend."""
    return _ABCD_DIR


def _ensure_abcd_path() -> None:
    """Load ABCD modules from the bundled ``abcd_original`` directory."""
    required_files = (
        "configuration.py",
        "models.py",
        os.path.join("features_repository", "feature_configs_handler.py"),
    )
    missing = [
        relative_path
        for relative_path in required_files
        if not os.path.isfile(os.path.join(_ABCD_DIR, relative_path))
    ]
    if missing:
        raise AbcdConfigError(
            "Bundled ABCD source is incomplete. Missing: " + ", ".join(missing)
        )

    # Keep the bundled detector ahead of site-packages and other local copies.
    while _ABCD_DIR in sys.path:
        sys.path.remove(_ABCD_DIR)
    sys.path.insert(0, _ABCD_DIR)


def _retry_with_backoff(
    func: Callable[[], T],
    *,
    max_retries: int = 3,
    backoff_factor: float = 2.0,
) -> T:
    """Retry transient Google authentication/network transport failures."""
    last_error: TransportError | None = None
    for attempt in range(max_retries):
        try:
            return func()
        except TransportError as exc:
            last_error = exc
            if attempt >= max_retries - 1:
                break
            wait_seconds = backoff_factor**attempt
            logger.warning(
                "Google transport error (attempt %d/%d); retrying in %.1fs: %s",
                attempt + 1,
                max_retries,
                wait_seconds,
                exc,
            )
            time.sleep(wait_seconds)

    raise AbcdExternalServiceError(
        "Could not reach Google authentication/services after "
        f"{max_retries} attempts: {last_error}"
    ) from last_error


def _normalise_list_str(values: Optional[List[str]]) -> str:
    """Convert an optional list into a unique comma-separated prompt value."""
    if not values:
        return ""

    seen: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
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
    """Build the real detector ``Configuration`` for one job."""
    _ensure_abcd_path()
    from configuration import Configuration
    from features_repository import feature_configs_handler
    from models import CreativeProviderType, VideoFeatureCategory

    normalized_format = (creative_format or "auto").strip().lower()
    if normalized_format not in {"long_form", "shorts", "auto"}:
        raise AbcdConfigError(
            "creative_format must be 'long_form', 'shorts', or 'auto'."
        )

    is_youtube = "youtube.com" in video_uri or "youtu.be" in video_uri
    creative_provider_type = (
        CreativeProviderType.YOUTUBE.value
        if is_youtube
        else CreativeProviderType.GCS.value
    )

    bucket_name = (settings.GCS_BUCKET or "placeholder").strip() or "placeholder"
    use_annotations = settings.ABCD_USE_ANNOTATIONS and not is_youtube
    use_llms = settings.ABCD_USE_LLMS
    run_long_form = settings.ABCD_RUN_LONG_FORM
    run_shorts = settings.ABCD_RUN_SHORTS

    if normalized_format == "long_form":
        run_long_form = True
        run_shorts = False
    elif normalized_format == "shorts":
        run_long_form = False
        run_shorts = True

    if advanced:
        if advanced.get("enable_llms") is not None:
            use_llms = bool(advanced["enable_llms"])
        if advanced.get("enable_annotations") is not None and not is_youtube:
            use_annotations = bool(advanced["enable_annotations"])

    if advanced and isinstance(advanced.get("features_to_evaluate"), list):
        features_to_evaluate = [
            str(feature_id)
            for feature_id in advanced["features_to_evaluate"]
            if str(feature_id).strip()
        ]
    else:
        features_to_evaluate: list[str] = []
        if run_long_form:
            features_to_evaluate.extend(
                feature.id
                for feature in feature_configs_handler.features_configs_handler
                .get_feature_configs_by_category(
                    VideoFeatureCategory.LONG_FORM_ABCD
                )
            )
        if run_shorts:
            features_to_evaluate.extend(
                feature.id
                for feature in feature_configs_handler.features_configs_handler
                .get_feature_configs_by_category(VideoFeatureCategory.SHORTS)
            )

    config = Configuration()
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
        extract_brand_metadata=settings.ABCD_EXTRACT_BRAND_METADATA,
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
    """Run the real bundled ABCD pipeline for exactly one video."""
    _ensure_abcd_path()
    import models
    from creative_providers import creative_provider_proto
    from creative_providers import creative_provider_registry
    from evaluation_services import video_evaluation_service
    from helpers import generic_helpers

    try:
        creative_provider: creative_provider_proto.CreativeProviderProto = (
            creative_provider_registry.provider_factory.get_provider(
                config.creative_provider_type.value
            )
        )
        video_uris = list(creative_provider.get_creative_uris(config) or [])
    except Exception as exc:
        raise AbcdEngineError(f"Failed to resolve creative URI: {exc}") from exc

    if not video_uris:
        raise AbcdConfigError("No video URI was returned by the creative provider.")

    resolved_video_uri = video_uris[0]
    if (
        config.creative_provider_type == models.CreativeProviderType.GCS
        and not resolved_video_uri.startswith("gs://")
    ):
        raise AbcdConfigError(
            f"GCS analysis requires a gs:// URI, got: {resolved_video_uri}"
        )
    if (
        config.creative_provider_type == models.CreativeProviderType.YOUTUBE
        and "youtube.com" not in resolved_video_uri
        and "youtu.be" not in resolved_video_uri
    ):
        raise AbcdConfigError(
            "YouTube analysis requires a youtube.com or youtu.be URL."
        )

    try:
        if (
            config.use_annotations
            and config.creative_provider_type == models.CreativeProviderType.GCS
        ):
            try:
                from annotations_evaluation import annotations_generation

                annotations_generation.generate_video_annotations(
                    config, resolved_video_uri
                )
            except Exception as exc:
                raise AbcdExternalServiceError(
                    f"Video annotations generation failed: {exc}"
                ) from exc

        # The custom Shorts repository also contains FIRST_5_SECS_VIDEO features.
        if (
            (config.run_long_form_abcd or config.run_shorts)
            and config.creative_provider_type == models.CreativeProviderType.GCS
        ):
            try:
                generic_helpers.trim_video(config, resolved_video_uri)
            except Exception as exc:
                raise AbcdEngineError(
                    f"Failed to create the first-five-second video: {exc}"
                ) from exc

        long_form_evaluations = []
        shorts_evaluations = []

        if config.run_long_form_abcd:
            try:
                long_form_evaluations = _retry_with_backoff(
                    lambda: video_evaluation_service.video_evaluation_service
                    .evaluate_features(
                        config=config,
                        video_uri=resolved_video_uri,
                        features_category=(
                            models.VideoFeatureCategory.LONG_FORM_ABCD
                        ),
                    )
                )
            except AbcdExternalServiceError:
                raise
            except Exception as exc:
                raise AbcdExternalServiceError(
                    f"Long-form feature evaluation failed: {exc}"
                ) from exc

        if config.run_shorts:
            try:
                shorts_evaluations = _retry_with_backoff(
                    lambda: video_evaluation_service.video_evaluation_service
                    .evaluate_features(
                        config=config,
                        video_uri=resolved_video_uri,
                        features_category=models.VideoFeatureCategory.SHORTS,
                    )
                )
            except AbcdExternalServiceError:
                raise
            except Exception as exc:
                raise AbcdExternalServiceError(
                    f"Shorts feature evaluation failed: {exc}"
                ) from exc

        return models.VideoAssessment(
            brand_name=config.brand_name,
            video_uri=resolved_video_uri,
            long_form_abcd_evaluated_features=long_form_evaluations,
            shorts_evaluated_features=shorts_evaluations,
            config=config,
        )
    finally:
        try:
            generic_helpers.remove_local_video_files()
        except Exception as exc:  # pragma: no cover - cleanup only
            logger.warning("Could not clean local video buffers: %s", exc)


def _is_feature_included_in_score(feature_eval: Any) -> bool:
    """Return whether an evaluated feature should affect the score."""
    feature = getattr(feature_eval, "feature", None)
    return bool(getattr(feature, "include_in_evaluation", True))


def _get_scoreable_features(feature_evaluations: List[Any]) -> List[Any]:
    """Filter evaluations using the feature's include_in_evaluation flag."""
    return [
        feature_eval
        for feature_eval in feature_evaluations
        if _is_feature_included_in_score(feature_eval)
    ]


def _feature_result_from_abcd(feature_eval: Any) -> FeatureResult:
    """Map a real ABCD ``FeatureEvaluation`` to the frontend API schema."""
    detected = bool(getattr(feature_eval, "detected", False))
    rationale = getattr(feature_eval, "rationale", "") or ""
    evidence = getattr(feature_eval, "evidence", "") or ""
    feature = getattr(feature_eval, "feature", None)

    details = " ".join(part for part in (rationale, evidence) if part).strip()
    return FeatureResult(
        feature_id=getattr(feature, "id", ""),
        feature_name=getattr(feature, "name", ""),
        result="Excellent" if detected else "Needs Review",
        details=details or None,
    )


def _overall_result_from_score(score_pct: float) -> str:
    """Map the binary adherence percentage to the existing UI label."""
    if score_pct >= 80:
        return "Excellent"
    if score_pct >= 65:
        return "Might Improve"
    return "Needs Review"


def _select_active_score_features(
    creative_format: str,
    scoreable_long_form: List[Any],
    scoreable_shorts: List[Any],
) -> List[Any]:
    """Choose the score source based on the requested creative format."""
    if creative_format == "shorts":
        return scoreable_shorts
    if creative_format == "long_form":
        return scoreable_long_form
    return scoreable_long_form + scoreable_shorts


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
    """Run real ABCD analysis and build the payload consumed by the frontend."""
    _ensure_abcd_path()
    normalized_format = (creative_format or "auto").strip().lower()
    start = time.monotonic()

    logger.info(
        "ABCD run starting: format=%s, source=%s, detector=%s",
        normalized_format,
        video_uri,
        _ABCD_DIR,
    )

    config = _build_abcd_config(
        video_uri,
        brand_name,
        brand_variations=brand_variations,
        products=products,
        product_categories=product_categories,
        call_to_actions=call_to_actions,
        creative_format=normalized_format,
        advanced=advanced,
    )

    try:
        assessment = _run_single_video_assessment(config, video_uri)
    except (AbcdConfigError, AbcdExternalServiceError, AbcdEngineError):
        logger.exception("ABCD analysis failed for %s", video_uri)
        raise
    except Exception as exc:
        logger.exception("Unexpected ABCD error for %s", video_uri)
        raise AbcdEngineError(f"Unexpected error in ABCD engine: {exc}") from exc

    long_form_evaluated = list(
        assessment.long_form_abcd_evaluated_features or []
    )
    shorts_evaluated = list(assessment.shorts_evaluated_features or [])

    scoreable_long_form = _get_scoreable_features(long_form_evaluated)
    scoreable_shorts = _get_scoreable_features(shorts_evaluated)

    # Long Form currently has no excluded criteria. Shorts is intentionally
    # analyzed with 23 criteria but only the 17 scoreable results are returned
    # to the frontend and included in score/pillar denominators.
    long_form_results = [
        _feature_result_from_abcd(feature_eval)
        for feature_eval in scoreable_long_form
    ]
    shorts_results = [
        _feature_result_from_abcd(feature_eval)
        for feature_eval in scoreable_shorts
    ]

    active_score_features = _select_active_score_features(
        normalized_format,
        scoreable_long_form,
        scoreable_shorts,
    )
    total = len(active_score_features)
    passed = sum(
        1
        for feature_eval in active_score_features
        if bool(getattr(feature_eval, "detected", False))
    )
    score_pct = (passed * 100.0 / total) if total else 0.0
    overall_result = _overall_result_from_score(score_pct)

    _ensure_abcd_path()
    from models import VideoFeatureSubCategory

    subcategory_to_letter = {
        "ATTRACT": ("A", "Attract"),
        "BRAND": ("B", "Brand"),
        "CONNECT": ("C", "Connect"),
        "DIRECT": ("D", "Direct"),
    }
    pillar_counts: Dict[str, Dict[str, Any]] = {
        "A": {"name": "Attract", "passed": 0, "total": 0},
        "B": {"name": "Brand", "passed": 0, "total": 0},
        "C": {"name": "Connect", "passed": 0, "total": 0},
        "D": {"name": "Direct", "passed": 0, "total": 0},
    }

    for feature_eval in active_score_features:
        feature = getattr(feature_eval, "feature", None)
        subcategory = getattr(feature, "sub_category", None)
        if subcategory is None:
            continue

        if isinstance(subcategory, VideoFeatureSubCategory):
            subcategory_name = subcategory.name
        else:
            subcategory_name = str(subcategory).split(".")[-1]

        mapping = subcategory_to_letter.get(subcategory_name)
        if mapping is None:
            continue

        letter, _ = mapping
        pillar_counts[letter]["total"] += 1
        if bool(getattr(feature_eval, "detected", False)):
            pillar_counts[letter]["passed"] += 1

    pillar_scores: List[AbcdPillarScore] = []
    for letter, info in pillar_counts.items():
        if not info["total"]:
            continue
        pillar_pct = info["passed"] * 100.0 / info["total"]
        pillar_scores.append(
            AbcdPillarScore(
                letter=letter,
                name=info["name"],
                score_pct=round(pillar_pct, 2),
                passed=info["passed"],
                total=info["total"],
                result=_overall_result_from_score(pillar_pct),
            )
        )

    duration_seconds = time.monotonic() - start
    logger.info(
        "ABCD run complete: format=%s, long_analyzed=%d, shorts_analyzed=%d, "
        "shorts_scored=%d, excluded=%d, score=%0.2f (%d/%d), duration=%0.2fs",
        normalized_format,
        len(long_form_evaluated),
        len(shorts_evaluated),
        len(scoreable_shorts),
        len(shorts_evaluated) - len(scoreable_shorts),
        score_pct,
        passed,
        total,
        duration_seconds,
    )

    return JobResultPayload(
        video_uri=assessment.video_uri,
        brand_name=assessment.brand_name,
        result_source="abcd",
        overall_score_pct=round(score_pct, 2),
        overall_result=overall_result,
        abcd_pillar_scores=pillar_scores,
        long_form_abcd=long_form_results,
        shorts=shorts_results,
    )


def get_shorts_feature_config_summary() -> list[dict[str, Any]]:
    """Return configured Shorts features and whether each contributes to score."""
    _ensure_abcd_path()
    from features_repository import feature_configs_handler
    from models import VideoFeatureCategory

    features = (
        feature_configs_handler.features_configs_handler
        .get_feature_configs_by_category(VideoFeatureCategory.SHORTS)
    )
    return [
        {
            "feature_id": feature.id,
            "feature_name": feature.name,
            "included_in_score": bool(
                getattr(feature, "include_in_evaluation", True)
            ),
            "subcategory": getattr(
                getattr(feature, "sub_category", None), "value", ""
            ),
        }
        for feature in features
    ]


def is_real_abcd_available() -> bool:
    """Return True only when real GCP execution and bundled source are ready."""
    return bool(
        settings.USE_REAL_ABCD
        and settings.GCP_PROJECT_ID
        and os.path.isfile(os.path.join(_ABCD_DIR, "configuration.py"))
    )

# NEW CODE ABOVE 

# OLD CODE BELOW 
#  """
# Phase 2: Run the real ABCD detector for one video (YouTube URL or GCS URI).
# Builds config from env + job, runs one-video pipeline, maps result to our API shape.
# """
# import logging
# import os
# import sys
# import time
# from typing import Any, Dict, List, Optional

# from config import settings
# from schemas import AbcdPillarScore, FeatureResult, JobResultPayload

# logger = logging.getLogger(__name__)


# class AbcdConfigError(Exception):
#     """Configuration/parameter problems before calling external services."""


# class AbcdExternalServiceError(Exception):
#     """Errors from external services (GCP APIs, network, etc.)."""


# class AbcdEngineError(Exception):
#     """Errors inside the abcd_original engine (unexpected states)."""

# # Path to abcd_original so ABCD's imports resolve
# _BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
# _ABCD_DIR = os.path.join(_BACKEND_DIR, "abcd_original")


# def _ensure_abcd_path():
#     """Prepend abcd_original to sys.path so 'import configuration' etc. load from there."""
#     if _ABCD_DIR not in sys.path:
#         sys.path.insert(0, _ABCD_DIR)


# def _normalise_list_str(values: Optional[List[str]]) -> str:
#     if not values:
#         return ""
#     # Join unique, non-empty strings with comma + space to keep prompts readable.
#     seen = []
#     for v in values:
#         v = (v or "").strip()
#         if v and v not in seen:
#             seen.append(v)
#     return ", ".join(seen)


# def _build_abcd_config(
#     video_uri: str,
#     brand_name: str,
#     *,
#     brand_variations: Optional[List[str]] = None,
#     products: Optional[List[str]] = None,
#     product_categories: Optional[List[str]] = None,
#     call_to_actions: Optional[List[str]] = None,
#     creative_format: Optional[str] = None,
#     advanced: Optional[Dict[str, Any]] = None,
# ) -> "Configuration":
#     """Build ABCD Configuration from our settings and job. Requires _ensure_abcd_path() first."""
#     _ensure_abcd_path()
#     from configuration import Configuration
#     from models import CreativeProviderType
#     from features_repository import feature_configs_handler

#     config = Configuration()

#     # Determine creative provider type
#     is_youtube = "youtube.com" in video_uri or "youtu.be" in video_uri
#     creative_provider_type = CreativeProviderType.YOUTUBE.value if is_youtube else CreativeProviderType.GCS.value

#     # Bucket: required by set_parameters; use env or placeholder for YouTube
#     bucket_name = (settings.GCS_BUCKET or "placeholder").strip() or "placeholder"

#     # Feature ids: evaluate all features by default (may be overridden by advanced options)
#     all_features = feature_configs_handler.features_configs_handler.get_all_features()
#     default_features_to_evaluate = [f.id for f in all_features]

#     features_to_evaluate = default_features_to_evaluate
#     use_annotations = settings.ABCD_USE_ANNOTATIONS and not is_youtube
#     use_llms = settings.ABCD_USE_LLMS
#     run_long_form = settings.ABCD_RUN_LONG_FORM
#     run_shorts = settings.ABCD_RUN_SHORTS

#     if advanced:
#         # features_to_evaluate: explicit override if provided
#         if isinstance(advanced.get("features_to_evaluate"), list) and advanced["features_to_evaluate"]:
#             features_to_evaluate = [str(fid) for fid in advanced["features_to_evaluate"]]
#         # enable_llms / enable_annotations override env defaults if not None
#         if advanced.get("enable_llms") is not None:
#             use_llms = bool(advanced["enable_llms"])
#         if advanced.get("enable_annotations") is not None and not is_youtube:
#             use_annotations = bool(advanced["enable_annotations"])

#     # Creative format overrides long_form/shorts toggles if provided
#     if creative_format == "long_form":
#         run_long_form = True
#         run_shorts = False
#     elif creative_format == "shorts":
#         run_long_form = False
#         run_shorts = True

#     config.set_parameters(
#         project_id=settings.GCP_PROJECT_ID or "placeholder",
#         project_zone=settings.GCP_REGION,
#         bucket_name=bucket_name,
#         knowledge_graph_api_key=settings.KNOWLEDGE_GRAPH_API_KEY,
#         bigquery_dataset="",
#         bigquery_table="",
#         assessment_file="",
#         use_annotations=use_annotations,
#         use_llms=use_llms,
#         extract_brand_metadata=True,
#         run_long_form_abcd=run_long_form,
#         run_shorts=run_shorts,
#         features_to_evaluate=features_to_evaluate,
#         creative_provider_type=creative_provider_type,
#         verbose=False,
#     )
#     config.set_videos([video_uri])
#     config.set_brand_details(
#         brand_name=brand_name or "Brand",
#         brand_variations=_normalise_list_str(brand_variations),
#         products=_normalise_list_str(products),
#         products_categories=_normalise_list_str(product_categories),
#         call_to_actions=_normalise_list_str(call_to_actions),
#     )
#     return config


# def _run_single_video_assessment(config: "Configuration", video_uri: str):
#     """Run ABCD pipeline for one video; return VideoAssessment. Requires _ensure_abcd_path()."""
#     _ensure_abcd_path()
#     import models
#     from creative_providers import creative_provider_registry
#     from creative_providers import creative_provider_proto
#     from evaluation_services import video_evaluation_service
#     from helpers import generic_helpers

#     try:
#         creative_provider: creative_provider_proto.CreativeProviderProto = (
#             creative_provider_registry.provider_factory.get_provider(config.creative_provider_type.value)
#         )
#         # get_creative_uris may return a generator; normalize to a list
#         video_uris = list(creative_provider.get_creative_uris(config) or [])
#     except Exception as exc:
#         raise AbcdEngineError(f"Failed to resolve creative URIs: {exc}") from exc

#     if not video_uris:
#         raise AbcdConfigError("No video URIs returned from creative provider")

#     video_uri = video_uris[0]

#     if config.creative_provider_type == models.CreativeProviderType.GCS and "gs://" not in video_uri:
#         raise ValueError(f"GCS creative provider requires gs:// URI, got {video_uri}")
#     if config.creative_provider_type == models.CreativeProviderType.YOUTUBE and "youtube.com" not in video_uri and "youtu.be" not in video_uri:
#         raise ValueError(f"YouTube creative provider requires YouTube URL, got {video_uri}")

#     # Annotations only for GCS
#     if config.use_annotations and config.creative_provider_type == models.CreativeProviderType.GCS:
#         try:
#             from annotations_evaluation import annotations_generation

#             annotations_generation.generate_video_annotations(config, video_uri)
#         except Exception as exc:
#             raise AbcdExternalServiceError(f"Video annotations generation failed: {exc}") from exc

#     # Trim first 5s for long-form (GCS only)
#     if config.run_long_form_abcd and config.creative_provider_type == models.CreativeProviderType.GCS:
#         try:
#             generic_helpers.trim_video(config, video_uri)
#         except Exception as exc:
#             raise AbcdEngineError(f"Failed to trim video for long-form ABCD: {exc}") from exc

#     long_form_abcd_evaluated_features = []
#     shorts_evaluated_features = []

#     if config.run_long_form_abcd:
#         try:
#             long_form_abcd_evaluated_features = (
#                 video_evaluation_service.video_evaluation_service.evaluate_features(
#                     config=config,
#                     video_uri=video_uri,
#                     features_category=models.VideoFeatureCategory.LONG_FORM_ABCD,
#                 )
#             )
#         except Exception as exc:
#             raise AbcdExternalServiceError(f"Long-form feature evaluation failed: {exc}") from exc
#     if config.run_shorts:
#         try:
#             shorts_evaluated_features = (
#                 video_evaluation_service.video_evaluation_service.evaluate_features(
#                     config=config,
#                     video_uri=video_uri,
#                     features_category=models.VideoFeatureCategory.SHORTS,
#                 )
#             )
#         except Exception as exc:
#             raise AbcdExternalServiceError(f"Shorts feature evaluation failed: {exc}") from exc

#     video_assessment = models.VideoAssessment(
#         brand_name=config.brand_name,
#         video_uri=video_uri,
#         long_form_abcd_evaluated_features=long_form_abcd_evaluated_features,
#         shorts_evaluated_features=shorts_evaluated_features,
#         config=config,
#     )

#     # Cleanup local files if any (e.g. from trim)
#     try:
#         generic_helpers.remove_local_video_files()
#     except Exception as e:
#         logger.warning("Cleanup local video files: %s", e)

#     return video_assessment


# def _feature_result_from_abcd(feature_eval) -> FeatureResult:
#     """Map ABCD FeatureEvaluation to our FeatureResult.
#     Follows standard ABCD engine: per-feature is binary (passed/failed).
#     - detected=True  → Excellent (feature passed)
#     - detected=False → Needs Review (feature failed)
#     """
#     detected = getattr(feature_eval, "detected", False)
#     rationale = getattr(feature_eval, "rationale", "") or ""
#     feature = getattr(feature_eval, "feature", None)
#     name = feature.name if feature else ""
#     fid = feature.id if feature else ""

#     result_label = "Excellent" if detected else "Needs Review"

#     details = rationale
#     if getattr(feature_eval, "evidence", None):
#         details = (details + " " + (feature_eval.evidence or "")).strip()

#     return FeatureResult(
#         feature_id=fid,
#         feature_name=name,
#         result=result_label,
#         details=details or None,
#     )


# def _overall_result_from_score(score_pct: float) -> str:
#     """ABCD standard: score >= 80 Excellent, 65–80 Might Improve, < 65 Needs Review."""
#     if score_pct >= 80:
#         return "Excellent"
#     if score_pct >= 65:
#         return "Might Improve"
#     return "Needs Review"


# def run_abcd_analysis(
#     video_uri: str,
#     brand_name: str,
#     *,
#     brand_variations: Optional[List[str]] = None,
#     products: Optional[List[str]] = None,
#     product_categories: Optional[List[str]] = None,
#     call_to_actions: Optional[List[str]] = None,
#     creative_format: Optional[str] = None,
#     advanced: Optional[Dict[str, Any]] = None,
# ) -> JobResultPayload:
#     """
#     Run ABCD detector for one video (YouTube URL or GCS URI).
#     Returns our API result payload. Raises on error.
#     """
#     _ensure_abcd_path()
#     start = time.monotonic()
#     logger.info(
#         "ABCD run start",
#         extra={
#             "event": "abcd_start",
#             "video_uri": video_uri,
#             "brand_name": brand_name,
#             "creative_format": creative_format,
#         },
#     )
#     config = _build_abcd_config(
#         video_uri,
#         brand_name,
#         brand_variations=brand_variations,
#         products=products,
#         product_categories=product_categories,
#         call_to_actions=call_to_actions,
#         creative_format=creative_format,
#         advanced=advanced,
#     )
#     try:
#         assessment = _run_single_video_assessment(config, video_uri)
#     except AbcdConfigError:
#         # Config errors are logged and re-raised as-is for clearer job errors.
#         logger.exception("ABCD config error for video %s", video_uri)
#         raise
#     except AbcdExternalServiceError:
#         logger.exception("ABCD external service error for video %s", video_uri)
#         raise
#     except AbcdEngineError:
#         logger.exception("ABCD engine error for video %s", video_uri)
#         raise
#     except Exception as exc:
#         logger.exception("ABCD unexpected error for video %s", video_uri)
#         raise AbcdEngineError(f"Unexpected error in ABCD engine: {exc}") from exc

#     all_evaluated = list(assessment.long_form_abcd_evaluated_features) + list(
#         assessment.shorts_evaluated_features
#     )
#     long_form = [
#         _feature_result_from_abcd(fe) for fe in assessment.long_form_abcd_evaluated_features
#     ]
#     shorts = [
#         _feature_result_from_abcd(fe) for fe in assessment.shorts_evaluated_features
#     ]

#     # Overall score (ABCD standard): % of features detected, then Excellent / Might Improve / Needs Review
#     total = len(all_evaluated)
#     passed = sum(1 for fe in all_evaluated if getattr(fe, "detected", False))
#     score_pct = (passed * 100.0 / total) if total else 0.0
#     overall_result = _overall_result_from_score(score_pct)

#     # Per-pillar ABCD scores (A/B/C/D) based on long-form features only.
#     _ensure_abcd_path()
#     from models import VideoFeatureSubCategory  # type: ignore

#     subcat_to_letter = {
#         "ATTRACT": ("A", "Attract"),
#         "BRAND": ("B", "Brand"),
#         "CONNECT": ("C", "Connect"),
#         "DIRECT": ("D", "Direct"),
#     }
#     pillar_counts: Dict[str, Dict[str, Any]] = {
#         "A": {"name": "Attract", "passed": 0, "total": 0},
#         "B": {"name": "Brand", "passed": 0, "total": 0},
#         "C": {"name": "Connect", "passed": 0, "total": 0},
#         "D": {"name": "Direct", "passed": 0, "total": 0},
#     }

#     for fe in assessment.long_form_abcd_evaluated_features:
#         feature = getattr(fe, "feature", None)
#         subcat = getattr(feature, "sub_category", None)
#         if not subcat:
#             continue
#         key_name = None
#         if isinstance(subcat, VideoFeatureSubCategory):
#             key_name = subcat.name
#         else:
#             key_name = str(subcat)
#         mapping = subcat_to_letter.get(key_name)
#         if not mapping:
#             continue
#         letter, _ = mapping
#         pillar_counts[letter]["total"] += 1
#         if getattr(fe, "detected", False):
#             pillar_counts[letter]["passed"] += 1

#     pillar_scores: List[AbcdPillarScore] = []
#     for letter, info in pillar_counts.items():
#         if not info["total"]:
#             continue
#         pillar_pct = info["passed"] * 100.0 / info["total"]
#         pillar_scores.append(
#             AbcdPillarScore(
#                 letter=letter,
#                 name=info["name"],
#                 score_pct=round(pillar_pct, 2),
#                 passed=info["passed"],
#                 total=info["total"],
#                 result=_overall_result_from_score(pillar_pct),
#             )
#         )

#     duration_sec = time.monotonic() - start
#     logger.info(
#         "ABCD run complete",
#         extra={
#             "event": "abcd_complete",
#             "video_uri": assessment.video_uri,
#             "brand_name": assessment.brand_name,
#             "overall_score_pct": round(score_pct, 2),
#             "overall_result": overall_result,
#             "duration_sec": round(duration_sec, 2),
#             "total_features": total,
#             "passed_features": passed,
#         },
#     )

#     return JobResultPayload(
#         video_uri=assessment.video_uri,
#         brand_name=assessment.brand_name,
#         result_source="abcd",
#         overall_score_pct=round(score_pct, 2),
#         overall_result=overall_result,
#         abcd_pillar_scores=pillar_scores,
#         long_form_abcd=long_form,
#         shorts=shorts,
#     )


# def is_real_abcd_available() -> bool:
#     """True if GCP is configured and we should run real ABCD."""
#     return bool(settings.GCP_PROJECT_ID and settings.USE_REAL_ABCD)

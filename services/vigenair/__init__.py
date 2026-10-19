"""
ViGenAiR engine client for AdLens Video Variants.

Everything ViGenAiR-specific (bucket layout, file formats, Gemini prompts) lives in this
package; it knows nothing about AdLens users, credits or Mongo. routers/video_variant_jobs.py
applies the AdLens rules and calls these functions.

Engine contract (upstream @ 3d70141):
  <folder>/input.<ext>                 written by us → analysis starts
  <folder>/input.vtt, analysis.json    progress markers
  <folder>/data.json                   analysis done: scene list
  <folder>/error.txt                   folder name rejected
  <folder>/<ts>-combos/render.json     written by us → render starts
  <folder>/<ts>-combos/combos.json     render done: output files per variant × format
"""
import logging
import math
import os
import tempfile
import time
from typing import Dict, List, Optional

from config import settings
from services.vigenair import gcs
from services.vigenair.gcs import VigenairConfigError, VIDEO_EXTENSIONS
from services.vigenair.generation import GenerationError, generate_variants as _generate_variants
from services.vigenair.probe import ProbeError, probe_file, probe_gcs
from services.vigenair.prompts import ABCD_OBJECTIVES
from services.vigenair.transcode import downscale, needs_downscale

_log = logging.getLogger(__name__)

# data.json never changes once written, so keep the last few in memory
_SEGMENTS_CACHE: Dict[str, List[dict]] = {}

FORMATS = ["16:9", "9:16", "1:1", "3:4", "4:3"]
STATUS_PROGRESS = {"uploaded": 10, "transcribed": 40, "analysed": 75, "ready": 100, "failed": 100}

__all__ = [
    "VigenairConfigError", "ProbeError", "GenerationError", "VideoTooLongError",
    "FORMATS", "ABCD_OBJECTIVES",
    "probe_file", "probe_gcs", "import_video", "get_status", "get_segments",
    "generate_variants", "quote_render", "start_render", "get_render",
    "copy_render_file", "delete_video",
]


class VideoTooLongError(ValueError):
    pass


def _max_seconds() -> float:
    # 0.5 s tolerance: a "2:00" export often reports 120.04 s
    return settings.MAX_VIDEO_SECONDS + 0.5


def check_length(info: dict) -> None:
    if info["duration_s"] > _max_seconds():
        raise VideoTooLongError(
            f"Video is {info['duration_s']:.1f} s long. The maximum is {int(settings.MAX_VIDEO_SECONDS)} s."
        )


# ── Analysis ──────────────────────────────────────────────────────────────────

def import_video(source_uri: str, original_filename: str, user_ref: str, transcription: str = "g",
                 local_path: Optional[str] = None) -> dict:
    """
    Re-measure the video (never trust a caller's length), then put it in the engine bucket,
    which starts the analysis. Sources above 1080p are converted to 1080p first (see
    transcode.py); others are copied server-side. `local_path` is an already-downloaded copy
    of source_uri (the upload flow has one), used instead of downloading again.
    Returns {video_id, downscaled, engine_width, engine_height, ...probe info of the source}.
    """
    info = probe_file(local_path) if local_path else probe_gcs(source_uri)
    check_length(info)
    src_bucket, src_name = gcs.parse_gs_uri(source_uri)
    folder = gcs.make_folder_name(original_filename or src_name, user_ref, transcription)
    result = {"video_id": folder, "downscaled": False,
              "engine_width": info["width"], "engine_height": info["height"], **info}

    if needs_downscale(info):
        with tempfile.TemporaryDirectory(prefix="vv_ds_") as tmp:
            src = local_path
            if not src:
                src = os.path.join(tmp, "source")
                gcs.blob(src_bucket, src_name).download_to_filename(src)
            dst = os.path.join(tmp, "input.mp4")
            started = time.time()
            downscale(src, dst)
            out = probe_file(dst)
            _log.info("Downscaled %sx%s -> %sx%s in %.1f s for %s", info["width"], info["height"],
                      out["width"], out["height"], time.time() - started, folder)
            # Uploading input.mp4 is what starts the analysis
            gcs.blob(gcs.engine_bucket(), f"{folder}/input.mp4").upload_from_filename(dst, content_type="video/mp4")
        result.update(downscaled=True, engine_width=out["width"], engine_height=out["height"])
        return result

    ext = (src_name.rsplit(".", 1)[-1] if "." in src_name else "mp4").lower()
    if ext not in VIDEO_EXTENSIONS:
        ext = "mp4"
    gcs.copy(src_bucket, src_name, gcs.engine_bucket(), f"{folder}/input.{ext}")
    return result


def get_status(video_id: str) -> dict:
    """{status: uploaded|transcribed|analysed|ready|failed, progress, error}"""
    bucket = gcs.engine_bucket()
    names = set(gcs.list_names(bucket, f"{video_id}/", top_level_only=True))
    rel = {n[len(video_id) + 1:] for n in names}
    if "error.txt" in rel:
        msg = gcs.read_text(bucket, f"{video_id}/error.txt") or "Analysis failed"
        return {"status": "failed", "progress": 100, "error": msg.strip()[:300]}
    if "data.json" in rel:
        status = "ready"
    elif "analysis.json" in rel:
        status = "analysed"
    elif "input.vtt" in rel:
        status = "transcribed"
    else:
        status = "uploaded"
    return {"status": status, "progress": STATUS_PROGRESS[status], "error": None}


def _normalise_id(raw) -> str:
    # Same rules as upstream GenerationHelper.getAvSegments()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(int(raw) + 1)
    s = str(raw)
    return s[:-2] if s.endswith(".0") else s


def _load_segments(video_id: str) -> List[dict]:
    cached = _SEGMENTS_CACHE.get(video_id)
    if cached is None:
        data = gcs.read_json(gcs.engine_bucket(), f"{video_id}/data.json")
        if data is None:
            raise ValueError("Video analysis isn't finished yet")
        for seg in data:
            seg["av_segment_id"] = _normalise_id(seg["av_segment_id"])
        if len(_SEGMENTS_CACHE) > 200:
            _SEGMENTS_CACHE.clear()
        _SEGMENTS_CACHE[video_id] = cached = data
    return [dict(s) for s in cached]


def get_language(video_id: str) -> str:
    text = gcs.read_text(gcs.engine_bucket(), f"{video_id}/language.txt")
    return (text or "").strip() or "English"


def get_segments(video_id: str, with_media: bool = True) -> List[dict]:
    """Scene list for the UI, with signed thumbnail/clip URLs."""
    bucket = gcs.engine_bucket()
    segments = _load_segments(video_id)
    urls = gcs.sign_many(
        [(bucket, f"{video_id}/av_segments_cuts/{s['av_segment_id']}.{ext}", None) for s in segments for ext in ("jpg", "mp4")]
    ) if with_media else []
    out = []
    for i, seg in enumerate(segments):
        sid = seg["av_segment_id"]
        item = {
            "id": sid,
            "start_s": seg["start_s"],
            "end_s": seg["end_s"],
            "duration_s": round(seg["end_s"] - seg["start_s"], 2),
            "description": (seg.get("description") or "").strip(),
            "transcript": " ".join(seg.get("transcript") or []).strip(),
            "keywords": (seg.get("keywords") or "").strip(),
        }
        if with_media:
            item["thumbnail_url"], item["clip_url"] = urls[2 * i], urls[2 * i + 1]
        out.append(item)
    return out


def source_video_url(video_id: str) -> Optional[str]:
    bucket = gcs.engine_bucket()
    for name in gcs.list_names(bucket, f"{video_id}/input.", top_level_only=True):
        ext = name.rsplit(".", 1)[-1]
        if ext in VIDEO_EXTENSIONS:
            return gcs.signed_url(bucket, name)
    return None


def generate_variants(video_id: str, target_s: float, brief: str, objective: str) -> List[dict]:
    segments = _load_segments(video_id)
    return _generate_variants(segments, get_language(video_id), target_s, brief, objective)


# ── Render ────────────────────────────────────────────────────────────────────

def _clean_formats(formats: List[str]) -> List[str]:
    out = [f for f in FORMATS if f in set(formats or [])]
    if not out:
        raise ValueError("Pick at least one format")
    return out


def _resolve_variants(video_id: str, variants: List[dict]) -> List[dict]:
    """Keep only scene ids that exist in data.json, with times taken from data.json."""
    by_id = {s["av_segment_id"]: s for s in _load_segments(video_id)}
    resolved = []
    for i, v in enumerate(variants):
        scenes = sorted({str(s) for s in (v.get("scenes") or []) if str(s) in by_id}, key=float)
        if not scenes:
            raise ValueError(f"Cut {i + 1} has no valid scenes")
        resolved.append({
            "title": (v.get("title") or f"Cut {i + 1}").strip()[:120],
            "description": (v.get("description") or "").strip()[:2000],
            "score": float(v.get("score") or 0),
            "abcd": (v.get("abcd") or "").strip()[:8000],
            "segments": [by_id[s] for s in scenes],
            "duration_s": round(sum(by_id[s]["end_s"] - by_id[s]["start_s"] for s in scenes), 2),
        })
    return resolved


def credits_for(seconds: float) -> int:
    return max(1, math.ceil(round(seconds, 2) / settings.VIDEO_CREDIT_SECONDS))


def quote_render(video_id: str, variants: List[dict], formats: List[str]) -> dict:
    """One output file per cut × format; credits per file = ceil(length / VIDEO_CREDIT_SECONDS)."""
    fmts = _clean_formats(formats)
    files = []
    for i, v in enumerate(_resolve_variants(video_id, variants)):
        for f in fmts:
            files.append({
                "variant_index": i, "title": v["title"], "format": f,
                "duration_s": v["duration_s"], "credits": credits_for(v["duration_s"]),
            })
    return {
        "files": files,
        "total_seconds": round(sum(f["duration_s"] for f in files), 2),
        "credits": sum(f["credits"] for f in files),
    }


def start_render(video_id: str, variants: List[dict], formats: List[str], fade_out: bool = True) -> str:
    """Writes render.json (format per guide §2.5); returns render_id (the combos folder name)."""
    fmts = _clean_formats(formats)
    payload = []
    for i, v in enumerate(_resolve_variants(video_id, variants)):
        payload.append({
            "variant_id": i,
            "title": v["title"],
            "description": v["description"],
            "score": v["score"],
            "score_reasoning": v["abcd"],
            "av_segments": [
                {"av_segment_id": s["av_segment_id"], "start_s": s["start_s"], "end_s": s["end_s"]}
                for s in v["segments"]
            ],
            "render_settings": {
                "formats": fmts,
                "generate_text_assets": False,
                "generate_image_assets": False,
                "use_music_overlay": False,
                "use_continuous_audio": False,
                "fade_out": bool(fade_out),
                # Blurred bars instead of smart-crop (smart-crop needs the UI's crop files)
                "use_blanking_fill": True,
            },
        })
    render_id = f"{int(time.time() * 1000)}-combos"
    gcs.write_json(gcs.engine_bucket(), f"{video_id}/{render_id}/render.json", payload)
    return render_id


def get_render(video_id: str, render_id: str) -> dict:
    """{status: rendering|done, files: [{variant_index, title, format, blob, download_name}]}"""
    bucket = gcs.engine_bucket()
    prefix = f"{video_id}/{render_id}"
    combos = gcs.read_json(bucket, f"{prefix}/combos.json")
    if combos is None:
        done = len([n for n in gcs.list_names(bucket, f"{prefix}/") if n.endswith("_combos.json")])
        return {"status": "rendering", "variants_done": done, "files": []}
    files = []
    for key, combo in sorted(combos.items(), key=lambda kv: int(str(kv[0]).lstrip("_") or 0)):
        idx = int(str(combo.get("variant_id", key)).lstrip("_") or 0)
        title = combo.get("title") or f"Cut {idx + 1}"
        for fmt, out in (combo.get("variants") or {}).items():
            name = gcs.entity_to_blob_name(out.get("entity", ""), bucket)
            if not name:
                continue
            safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in title).strip().replace(" ", "_") or "cut"
            files.append({
                "variant_index": idx,
                "title": title,
                "format": fmt,
                "blob": name,
                "download_name": f"{safe_title}_{fmt.replace(':', 'x')}.mp4",
            })
    files.sort(key=lambda f: (f["variant_index"], FORMATS.index(f["format"]) if f["format"] in FORMATS else 9))
    return {"status": "done", "files": files}


def copy_render_file(video_id: str, blob_name: str, dest_bucket: str, dest_name: str) -> str:
    """Server-side copy of one rendered file into AdLens storage (for "Score with ABCD")."""
    if not blob_name.startswith(f"{video_id}/"):
        raise ValueError("File doesn't belong to this video")
    gcs.copy(gcs.engine_bucket(), blob_name, dest_bucket, dest_name)
    return f"gs://{dest_bucket}/{dest_name}"


def delete_video(video_id: str) -> int:
    if not video_id or "/" in video_id:
        raise ValueError("Invalid video id")
    return gcs.delete_prefix(gcs.engine_bucket(), f"{video_id}/")

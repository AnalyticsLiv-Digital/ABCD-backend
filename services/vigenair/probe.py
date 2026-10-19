"""
Measure a video (duration, size, codec) before anything is charged or copied.

Uses ffprobe when it's on PATH (the Docker image installs ffmpeg). Locally it falls back
to the ffmpeg binary bundled with imageio-ffmpeg (already a moviepy dependency).
A gs:// object is read through a short-lived signed URL, so only the header is fetched;
if signing isn't possible the file is downloaded to a temp dir instead.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

from services.vigenair import gcs

_log = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    """The file couldn't be read as a video."""


def _ffprobe(target: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
            "-of", "json", target,
        ],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise ProbeError(out.stderr.strip()[:300] or "ffprobe failed")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ProbeError("No video stream found")
    return {
        "duration_s": float((data.get("format") or {}).get("duration") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": video.get("codec_name"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _ffmpeg_info(target: str) -> dict:
    """Parse `ffmpeg -i` output (imageio-ffmpeg ships ffmpeg but not ffprobe)."""
    import imageio_ffmpeg

    out = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", target],
        capture_output=True, text=True, timeout=120,
    ).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    v = re.search(r"Stream #.*?Video:\s*([\w]+).*?,\s*(\d{2,5})x(\d{2,5})", out)
    if not m or not v:
        raise ProbeError("Could not read video duration")
    h, mi, s = m.groups()
    return {
        "duration_s": int(h) * 3600 + int(mi) * 60 + float(s),
        "width": int(v.group(2)),
        "height": int(v.group(3)),
        "codec": v.group(1),
        "has_audio": bool(re.search(r"Stream #.*?Audio:", out)),
    }


def probe_target(target: str) -> dict:
    try:
        info = _ffprobe(target) if shutil.which("ffprobe") else _ffmpeg_info(target)
    except (ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        # e.g. ffprobe reports duration "N/A" for some WebM files
        raise ProbeError(f"Could not read the video: {exc}") from exc
    if info["duration_s"] <= 0:
        raise ProbeError("Video has no duration")
    return info


def probe_file(path: str) -> dict:
    info = probe_target(path)
    info["size_bytes"] = os.path.getsize(path)
    return info


def probe_gcs(uri: str) -> dict:
    bucket, name = gcs.parse_gs_uri(uri)
    b = gcs.get_blob(bucket, name)
    if b is None:
        raise ProbeError("Video not found in storage")
    info: Optional[dict] = None
    try:
        info = probe_target(gcs.signed_url(bucket, name, minutes=10))
    except Exception as exc:
        _log.info("Header probe of %s failed (%s); downloading to probe", uri, exc)
    if info is None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, os.path.basename(name) or "video")
            b.download_to_filename(path)
            info = probe_target(path)
    info["size_bytes"] = int(b.size or 0)
    return info

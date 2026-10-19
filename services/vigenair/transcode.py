"""
Shrink sources larger than 1080p before they go to the engine.

ViGenAiR renders every output at the source's width (blur-fill scales by width), so a 4K
upload produced a 3840×6826 9:16 file. Converting the engine's copy to 1080p (short side)
keeps outputs at platform sizes (1920×1080, 1080×1920, 1080×1080) and makes analysis and
rendering faster. The original upload stays untouched in AdLens storage.
"""
import logging
import shutil
import subprocess

_log = logging.getLogger(__name__)

MAX_SHORT_SIDE = 1080


def needs_downscale(info: dict) -> bool:
    return min(info.get("width") or 0, info.get("height") or 0) > MAX_SHORT_SIDE


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled with moviepy; used locally on Windows

    return imageio_ffmpeg.get_ffmpeg_exe()


def downscale(src: str, dst: str, timeout_s: int = 600) -> None:
    """src → dst (MP4, H.264/AAC), short side MAX_SHORT_SIDE, index at the front (faststart)."""
    s = MAX_SHORT_SIDE
    vf = f"scale='if(gt(iw,ih),-2,{s})':'if(gt(iw,ih),{s},-2)':flags=lanczos"
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if out.returncode != 0:
        raise RuntimeError(f"Couldn't convert the video: {out.stderr.strip()[:300]}")

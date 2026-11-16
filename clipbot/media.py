"""Download a Twitch clip (yt-dlp) and render a 9:16 vertical version (ffmpeg)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(s: str) -> str:
    return _SAFE.sub("_", s).strip("_") or "clip"


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def have_ytdlp() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return shutil.which("yt-dlp") is not None


def download_clip(url: str, dest: Path, timeout: float = 180.0) -> Path | None:
    """Download `url` to `dest` (an .mp4 path). Returns the path or None."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        import yt_dlp  # type: ignore
        opts = {
            "outtmpl": str(dest.with_suffix("")) + ".%(ext)s",
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True, "noprogress": True,
            "socket_timeout": 30, "retries": 3,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except ImportError:
        cmd = ["yt-dlp", "-q", "--no-warnings", "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4",
               "-o", str(dest.with_suffix("")) + ".%(ext)s", url]
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.error("yt-dlp failed for %s: %s", url, e)
            return None
    except Exception as e:  # noqa: BLE001
        log.error("download failed for %s: %s", url, e)
        return None
    if dest.exists():
        return dest
    # yt-dlp may have chosen a different extension
    for cand in dest.parent.glob(dest.stem + ".*"):
        if cand.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            return cand
    log.error("download produced no file for %s", url)
    return None


def vertical_filter(width: int = 1080, height: int = 1920) -> str:
    """Blurred, zoomed background with the original clip letterboxed in the middle."""
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:5[bg];"
        f"[0:v]scale={width}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p"
    )


def render_vertical(src: Path, dest: Path, timeout: float = 300.0) -> Path | None:
    """Produce a 1080x1920 version of `src` for TikTok / Shorts / Reels."""
    if not have_ffmpeg():
        log.warning("ffmpeg not found; skipping vertical render")
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-filter_complex", vertical_filter(),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg failed: %s", e.stderr.decode(errors="replace")[-800:])
        return None
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out on %s", src)
        return None
    return dest

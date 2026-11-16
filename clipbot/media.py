"""Download a Twitch clip (yt-dlp) and render a 9:16 vertical version (ffmpeg)."""
from __future__ import annotations

import logging
import os
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


_ENCODERS: list[str] | None = None
_FAILED: set[str] = set()


def available_encoders() -> list[str]:
    """H.264 encoders this ffmpeg build lists, fastest hardware ones first."""
    global _ENCODERS
    if _ENCODERS is None:
        listed = ""
        try:
            listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                                    timeout=20).stdout
        except Exception:  # noqa: BLE001
            pass
        order = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox", "libx264"]
        _ENCODERS = [e for e in order if f" {e} " in listed] or ["libx264"]
    return [e for e in _ENCODERS if e not in _FAILED] or ["libx264"]


def encoder_args(encoder: str) -> list[str]:
    """Quality-oriented settings per encoder (30s clips: favour quality over speed)."""
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-bf", "2"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", "20", "-look_ahead", "0"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "19", "-qp_p", "21"]
    if encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-q:v", "65"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]


def run_encode(build_cmd, timeout: float, what: str) -> bool:
    """Run `build_cmd(encoder_args)` with the best encoder; on failure fall back to the next one."""
    for enc in list(available_encoders()):
        cmd = build_cmd(encoder_args(enc))
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode(errors="replace")[-600:]
            if enc != "libx264":
                log.warning("%s: encoder %s failed (%s); falling back", what, enc, err.strip().splitlines()[-1:] or err)
                _FAILED.add(enc)
                continue
            log.error("%s: ffmpeg failed: %s", what, err)
            return False
        except subprocess.TimeoutExpired:
            log.error("%s: ffmpeg timed out", what)
            return False
    return False


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


def render_vertical(src: Path, dest: Path, timeout: float = 300.0, ass_path: Path | None = None,
                    graph: str | None = None) -> Path | None:
    """Produce a 1080x1920 version of `src` for TikTok / Shorts / Reels.
    `graph` is the ffmpeg filter graph (default: blurred layout; see layout.py).
    If `ass_path` is given, those subtitles are burned in (libass)."""
    if not have_ffmpeg():
        log.warning("ffmpeg not found; skipping vertical render")
        return None
    src, dest = Path(src).resolve(), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base = graph or vertical_filter()
    vf = base
    cwd = None
    if ass_path is not None:
        # The ass filter parses ':' and '\' in its argument, so run ffmpeg inside the
        # subtitle's folder and pass only the (sanitised) file name.
        ass_path = Path(ass_path).resolve()
        cwd = str(ass_path.parent)
        vf = vf + f",ass={safe_name(ass_path.name)}"
        if safe_name(ass_path.name) != ass_path.name:
            log.warning("caption file name %s is not ffmpeg-safe; skipping captions", ass_path.name)
            vf, cwd = base, None
    def build(venc: list[str]) -> list[str]:
        return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-err_detect", "ignore_err",
                "-i", str(src), "-filter_complex", vf, *venc, "-r", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dest.resolve())]

    # run_encode cannot pass cwd, so chdir around it for the caption case
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        ok = run_encode(build, timeout, "vertical render")
    finally:
        os.chdir(old)
    return dest if ok and dest.exists() else None

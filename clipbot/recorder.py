"""Local clipping without any Twitch token.

`streamlink` (https://streamlink.github.io) pulls the public HLS stream of a
channel and pipes it into ffmpeg, which writes a rolling buffer of short
MPEG-TS segments on disk. When the detector fires, `cut()` concatenates the
segments covering the last N seconds into an MP4. Only the last
`buffer_seconds` of video are ever kept, so disk use stays flat.

    streamlink --stdout twitch.tv/<login> 720p60,...  |  ffmpeg -c copy -f segment ...

Both programs are plain subprocesses; the class only supervises them.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SEGMENT_SECONDS = 2          # Twitch keyframes every ~2s; -c copy can only cut there
DEFAULT_QUALITY = "best"


def have_streamlink() -> bool:
    return shutil.which("streamlink") is not None


@dataclass
class Segment:
    path: Path
    end: float           # unix time the segment was finished (file mtime)
    size: int
    start: float = 0.0   # filled in by list_segments from the previous segment's end


def list_segments(buffer_dir: Path) -> list[Segment]:
    """Segments oldest-first. Consecutive segments are contiguous, so each one
    starts where the previous ended; ffmpeg only splits on keyframes, so real
    segment lengths vary and the nominal SEGMENT_SECONDS is just a fallback."""
    out = []
    for p in Path(buffer_dir).glob("*.ts"):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > 0:
            out.append(Segment(p, st.st_mtime, st.st_size))
    out.sort(key=lambda s: (s.end, s.path.name))
    prev_end = None
    for s in out:
        if prev_end is not None and 0 < s.end - prev_end <= SEGMENT_SECONDS * 4:
            s.start = prev_end
        else:
            s.start = s.end - SEGMENT_SECONDS
        prev_end = s.end
    return out


def select_segments(segments: list[Segment], trigger_ts: float, pre: float, post: float) -> list[Segment]:
    """Segments that overlap [trigger_ts - pre, trigger_ts + post]."""
    lo, hi = trigger_ts - pre, trigger_ts + post
    return [s for s in segments if s.end > lo and s.start < hi]


def concat_segments(segments: list[Segment], dest: Path, timeout: float = 300.0,
                    trim_start: float = 0.0, duration: float | None = None, copy: bool = False) -> Path | None:
    """Join TS segments into an MP4. By default the video is re-encoded: Twitch
    segments carry ad splices and bitrate switches, and a stream-copied join
    of those plays back with broken frames ("Invalid NAL unit size"). Encoding
    also allows an exact start (`trim_start`) and `duration` instead of
    keyframe-bound cuts. `copy=True` restores the lossless join."""
    if not segments:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    listfile = dest.with_suffix(".txt")
    listfile.write_text("".join(f"file '{s.path.resolve().as_posix()}'\n" for s in segments), encoding="utf-8")
    head = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
            "-err_detect", "ignore_err", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    tail = ["-movflags", "+faststart", str(dest)]
    trim = (["-ss", f"{trim_start:.3f}"] if trim_start > 0 else []) + (["-t", f"{duration:.3f}"] if duration else [])
    try:
        if copy:
            cmd = head + trim + ["-c", "copy", "-bsf:a", "aac_adtstoasc"] + tail
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
            ok = True
        else:
            from .media import run_encode

            def build(venc: list[str]) -> list[str]:
                return head + trim + venc + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"] + tail

            ok = run_encode(build, timeout, "clip cut")
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg concat failed: %s", e.stderr.decode(errors="replace")[-800:])
        ok = False
    except subprocess.TimeoutExpired:
        log.error("ffmpeg concat timed out")
        ok = False
    finally:
        try:
            listfile.unlink()
        except OSError:
            pass
    return dest if ok and dest.exists() and dest.stat().st_size > 0 else None


class Recorder:
    """Keeps a rolling buffer of one channel's live stream on disk."""

    def __init__(self, login: str, buffer_dir: Path, buffer_seconds: float = 180.0,
                 quality: str = DEFAULT_QUALITY, source_url: str | None = None):
        self.login = login.lower()
        self.buffer_dir = Path(buffer_dir)
        self.buffer_seconds = buffer_seconds
        self.quality = quality
        self.source_url = source_url or f"https://www.twitch.tv/{self.login}"
        self._sl: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        self.closed = False

    # ----- status ---------------------------------------------------------
    def latest_segment_age(self) -> float | None:
        segs = list_segments(self.buffer_dir)
        return time.time() - segs[-1].end if segs else None

    def is_live(self, stale_after: float = 20.0) -> bool:
        age = self.latest_segment_age()
        return age is not None and age < stale_after

    # ----- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._supervise(), name=f"rec-{self.login}")

    async def stop(self) -> None:
        self.closed = True
        if self._task:
            self._task.cancel()
        self._kill()

    def _kill(self) -> None:
        for proc in (self._sl, self._ff):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        self._sl = self._ff = None

    def _spawn(self) -> None:
        sl_cmd = ["streamlink", "--stdout", "--loglevel", "error", "--retry-streams", "20", "--retry-max", "0",
                  "--hls-live-edge", "2", "--twitch-low-latency", self.source_url, self.quality]
        ff_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                  "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
                  "-reset_timestamps", "1", str(self.buffer_dir / "%08d.ts")]
        self._sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert self._sl.stdout is not None
        self._ff = subprocess.Popen(ff_cmd, stdin=self._sl.stdout, stderr=subprocess.PIPE)
        self._sl.stdout.close()   # let ffmpeg own the read end

    async def _supervise(self) -> None:
        backoff = 5.0
        while not self.closed:
            started = time.time()
            try:
                self._spawn()
                log.info("[%s] recording started (%s)", self.login, self.quality)
                while not self.closed and self._ff is not None and self._ff.poll() is None:
                    await asyncio.sleep(2)
                    self.prune()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] recorder error: %s", self.login, e)
            err = ""
            if self._sl and self._sl.stderr:
                try:
                    err = self._sl.stderr.read().decode(errors="replace").strip()[-300:]
                except Exception:  # noqa: BLE001
                    pass
            self._kill()
            if self.closed:
                break
            ran = time.time() - started
            backoff = 5.0 if ran > 120 else min(backoff * 2, 120.0)
            log.info("[%s] stream ended or offline%s; retrying in %.0fs", self.login,
                     f" ({err})" if err else "", backoff)
            await asyncio.sleep(backoff)

    # ----- buffer ---------------------------------------------------------
    def prune(self) -> None:
        cutoff = time.time() - self.buffer_seconds
        for s in list_segments(self.buffer_dir):
            if s.end < cutoff:
                try:
                    s.path.unlink()
                except OSError:
                    pass

    async def cut(self, trigger_ts: float, pre: float, post: float, dest: Path) -> Path | None:
        """Wait for the segment that covers `trigger_ts + post`, then write the clip."""
        deadline = time.time() + post + SEGMENT_SECONDS * 3
        while time.time() < deadline:
            segs = list_segments(self.buffer_dir)
            if segs and segs[-1].end >= trigger_ts + post + SEGMENT_SECONDS * 0.5:
                break
            await asyncio.sleep(0.5)
        chosen = select_segments(list_segments(self.buffer_dir), trigger_ts, pre, post)
        if not chosen:
            log.warning("[%s] no buffered video for the trigger; is the stream live?", self.login)
            return None
        covered = chosen[-1].end - chosen[0].start
        if covered < pre * 0.5:
            log.warning("[%s] only %.0fs buffered (wanted %.0fs); clipping what we have", self.login, covered, pre)
        # exact window inside the joined segments
        trim_start = max(0.0, (trigger_ts - pre) - chosen[0].start)
        return await asyncio.to_thread(concat_segments, chosen, dest, 300.0, trim_start, pre + post)

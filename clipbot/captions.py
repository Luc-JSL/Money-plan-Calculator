"""Auto-captions: transcribe a clip (faster-whisper) and write an ASS subtitle file
styled for 9:16 video, which ffmpeg then burns into the vertical render.

faster-whisper is optional (`pip install faster-whisper`); without it the bot
simply skips captions. The transcriber is injectable so tests need no model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

Word = tuple[float, float, str]          # start, end, text
Transcriber = Callable[[Path], Iterable[Word]]

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,84,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,0,2,60,60,440,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class CaptionLine:
    start: float
    end: float
    text: str


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def whisper_transcriber(model_size: str = "small") -> Transcriber:
    """Build a transcriber backed by faster-whisper (CPU, int8)."""
    from faster_whisper import WhisperModel  # type: ignore

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def run(path: Path) -> Iterable[Word]:
        segments, _ = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
        for seg in segments:
            for w in seg.words or []:
                yield (float(w.start), float(w.end), w.word.strip())

    return run


def group_words(words: Iterable[Word], max_words: int = 4, max_gap: float = 0.8,
                max_span: float = 2.5) -> list[CaptionLine]:
    """Chunk word timestamps into short on-screen lines. A line ends after
    `max_words`, on a pause longer than `max_gap`, or when it spans > `max_span`s."""
    lines: list[CaptionLine] = []
    chunk: list[str] = []
    start = end = 0.0
    for ws, we, text in words:
        text = " ".join(text.split())
        if not text:
            continue
        if chunk and (len(chunk) >= max_words or ws - end > max_gap or we - start > max_span):
            lines.append(CaptionLine(start, max(end, start + 0.3), " ".join(chunk)))
            chunk = []
        if not chunk:
            start = ws
        chunk.append(text)
        end = max(we, ws)
    if chunk:
        lines.append(CaptionLine(start, max(end, start + 0.3), " ".join(chunk)))
    return lines


def _ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ").upper()


def write_ass(lines: list[CaptionLine], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        for ln in lines:
            if ln.end <= ln.start:
                continue
            f.write(f"Dialogue: 0,{_ts(ln.start)},{_ts(ln.end)},Cap,,0,0,0,,{_escape(ln.text)}\n")
    return path


def make_captions(video: Path, ass_path: Path, transcriber: Transcriber | None = None,
                  model_size: str = "small") -> Path | None:
    """Transcribe `video` and write `ass_path`. Returns None when captions are
    unavailable or no speech was found. Never raises."""
    try:
        if transcriber is None:
            if not available():
                log.info("faster-whisper not installed; skipping captions")
                return None
            transcriber = whisper_transcriber(model_size)
        lines = group_words(transcriber(Path(video)))
    except Exception as e:  # noqa: BLE001 - captions are best-effort
        log.warning("captioning failed for %s: %s", video, e)
        return None
    if not lines:
        log.info("no speech detected in %s; no captions", video)
        return None
    write_ass(lines, ass_path)
    log.info("captions: %d lines -> %s", len(lines), ass_path)
    return Path(ass_path)

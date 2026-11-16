"""Vertical layouts for game-stream clips: facecam on top, gameplay below.

A Twitch frame is a wide game picture with the streamer's webcam pinned in a
corner. Cropping that to 9:16 the naive way loses the webcam, and the
"blurred bars" layout wastes most of the screen. The layouts here are the
formats gaming clips actually use on TikTok / Shorts:

  split    facecam band on top, the game zoomed to fill the rest (default)
  stack    facecam above the whole game frame, blurred fill above/below
  blurred  the whole game centred on a blurred copy (no facecam needed)

The facecam is found automatically with OpenCV's YuNet face detector plus
geometry (small, off-centre, pinned to an edge, does not move between
samples). The geometry rules follow NakhulGithesh/gta6-clipper (MIT).
OpenCV and the detector model are optional: without them, or when no
webcam is found, the clip falls back to the blurred layout.
"""
from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")   # silence "[h264 @ ...]" decoder chatter
import statistics
import subprocess
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]            # x, y, w, h in source pixels
Detector = Callable[["object"], Sequence[Box]]   # frame (BGR ndarray) -> face boxes

OUT_W, OUT_H = 1080, 1920
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")

# ----- geometry (what makes a detected face a pinned webcam inset) -----------
CORNER_MARGIN = 0.20        # within this fraction of an edge counts as anchored
MAX_FACE_HEIGHT = 0.35      # a face taller than this is the shot itself, not an inset
MIN_OFFSET = 0.18           # horizontal distance from the centre line
MIN_INSET_H, MAX_INSET_H = 0.10, 0.45
MAX_SPREAD = 0.05           # a pinned webcam does not move between samples
FACE_TO_INSET = 3.0         # webcam window is ~3 face widths wide
INSET_ASPECT = 16 / 9


def probe_size(path: Path) -> tuple[int, int]:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "json", str(path)], capture_output=True, text=True)
        s = json.loads(out.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:  # noqa: BLE001
        return 1920, 1080


def looks_like_inset(face: Box, fw: int, fh: int) -> bool:
    x, y, w, h = face
    if h > fh * MAX_FACE_HEIGHT:
        return False
    cx = (x + w / 2) / fw
    if abs(cx - 0.5) < MIN_OFFSET:
        return False
    return (x <= fw * CORNER_MARGIN or x + w >= fw * (1 - CORNER_MARGIN)
            or y <= fh * CORNER_MARGIN or y + h >= fh * (1 - CORNER_MARGIN))


def inset_from_face(face: Box, fw: int, fh: int) -> Box:
    """Grow a face box into the 16:9 window the webcam sits in."""
    x, y, w, h = face
    nw = min(fw, int(round(w * FACE_TO_INSET)))
    nh = min(fh, int(round(nw / INSET_ASPECT)))
    cx, cy = x + w / 2, y + h / 2
    nx = int(round(max(0, min(cx - nw / 2, fw - nw))))
    ny = int(round(max(0, min(cy - nh * 0.45, fh - nh))))
    return nx, ny, nw, nh


def stable_box(boxes: Sequence[Box], fw: int, fh: int, min_agree: int = 3) -> Box | None:
    """Median of boxes that agree on position; None if the 'webcam' moves around."""
    if len(boxes) < min_agree:
        return None
    cxs = [b[0] + b[2] / 2 for b in boxes]
    cys = [b[1] + b[3] / 2 for b in boxes]
    mx, my = statistics.median(cxs), statistics.median(cys)
    tol = fw * MAX_SPREAD
    close = [b for b, cx, cy in zip(boxes, cxs, cys) if abs(cx - mx) <= tol and abs(cy - my) <= tol]
    if len(close) < max(min_agree, 0.6 * len(boxes)):
        return None
    med = tuple(int(round(statistics.median(v))) for v in zip(*close))
    if not (MIN_INSET_H * fh <= med[3] <= MAX_INSET_H * fh):
        return None
    return med  # type: ignore[return-value]


def even(v: int) -> int:
    return max(2, int(v) - int(v) % 2)


# ----- detection ------------------------------------------------------------
def default_model_path(data_dir: Path) -> Path:
    return Path(os.environ.get("FACE_MODEL") or (Path(data_dir) / "models" / YUNET_FILE))


def download_model(dest: Path, timeout: float = 120.0) -> Path:
    import urllib.request
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(YUNET_URL, timeout=timeout) as r, tmp.open("wb") as f:
        f.write(r.read())
    if tmp.stat().st_size < 50_000:
        tmp.unlink()
        raise RuntimeError("downloaded model is too small; try again or download it manually")
    tmp.replace(dest)
    return dest


def yunet_detector(model_path: Path) -> Detector | None:
    try:
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
        import cv2  # type: ignore
    except ImportError:
        return None
    if not Path(model_path).is_file():
        return None
    try:
        det = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), 0.6, 0.3, 5000)
    except Exception as e:  # noqa: BLE001
        log.warning("could not load face model %s: %s", model_path, e)
        return None

    def detect(frame) -> list[Box]:
        det.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = det.detect(frame)
        if faces is None:
            return []
        return [tuple(int(round(float(v))) for v in f[:4]) for f in faces]  # type: ignore[misc]

    return detect


def sample_frames(video: Path, samples: int):
    """Yield (frame, width, height) for `samples` evenly spaced frames. Needs OpenCV."""
    import cv2  # type: ignore
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            return
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fw, fh = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if total <= 0 or not fw:
            return
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / samples))
            ok, frame = cap.read()
            if ok:
                yield frame, fw, fh
    finally:
        cap.release()


def find_facecam(video: Path, detector: Detector | None, samples: int = 10) -> Box | None:
    """The webcam inset rectangle in `video`, or None."""
    if detector is None:
        return None
    boxes: list[Box] = []
    fw = fh = 0
    try:
        for frame, fw, fh in sample_frames(Path(video), samples):
            for face in detector(frame):
                if looks_like_inset(face, fw, fh):
                    boxes.append(inset_from_face(face, fw, fh))
                    break
    except ImportError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("facecam detection failed on %s: %s", video, e)
        return None
    if len(boxes) < max(3, samples // 3):
        return None
    return stable_box(boxes, fw, fh)


def parse_box(text: str) -> Box | None:
    """'x,y,w,h' -> box, or None when empty/invalid."""
    try:
        parts = [int(p) for p in text.replace(" ", "").split(",") if p != ""]
    except ValueError:
        return None
    if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
        return None
    return tuple(parts)  # type: ignore[return-value]


# ----- ffmpeg filter graphs -------------------------------------------------
def blurred_graph() -> str:
    return (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"boxblur=20:5[bg];[0:v]scale={OUT_W}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p")


def split_graph(box: Box, facecam_share: float = 0.38) -> str:
    """Facecam band on top (facecam_share of the height), game zoomed to fill the rest."""
    x, y, w, h = box
    top_h = even(round(OUT_H * max(0.25, min(0.5, facecam_share))))
    game_h = OUT_H - top_h
    return (f"[0:v]crop={even(w)}:{even(h)}:{x}:{y},scale={OUT_W}:{top_h}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{top_h}[cam];"
            f"[0:v]scale={OUT_W}:{game_h}:force_original_aspect_ratio=increase,crop={OUT_W}:{game_h}[game];"
            f"[cam][game]vstack=inputs=2,format=yuv420p")


def stack_graph(box: Box) -> str:
    """Facecam above the whole game frame, both full width, blurred fill around them."""
    x, y, w, h = box
    cam_h = even(round(OUT_W * h / max(w, 1)))
    return (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"boxblur=20:5[bg];"
            f"[0:v]crop={even(w)}:{even(h)}:{x}:{y},scale={OUT_W}:{cam_h}[cam];"
            f"[0:v]scale={OUT_W}:-2[game];"
            f"[cam][game]vstack=inputs=2[block];"
            f"[bg][block]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p")


def choose_graph(layout: str, box: Box | None, facecam_share: float = 0.38) -> tuple[str, str]:
    """(filter graph, layout actually used)."""
    layout = (layout or "auto").lower()
    if layout == "blurred" or box is None:
        if layout in ("split", "stack", "auto") and box is None:
            log.info("no facecam found; using blurred layout")
        return blurred_graph(), "blurred"
    if layout in ("split", "auto"):
        return split_graph(box, facecam_share), "split"
    if layout == "stack":
        return stack_graph(box), "stack"
    return blurred_graph(), "blurred"

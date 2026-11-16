"""Keep the clips folder under a size / count budget by pruning the oldest clips.

A clip is a group of files sharing the same stem: `<stamp>-<id>.mp4`,
`<stamp>-<id>-vertical.mp4`, `<stamp>-<id>.ass`. Groups are removed whole,
oldest first, until both caps are satisfied. Files that do not start with a
`YYYYmmdd-HHMMSS` stamp are never touched.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_STAMP = re.compile(r"^(\d{8}-\d{6}-[A-Za-z0-9_-]+?)(?:-vertical)?\.(?:mp4|mkv|webm|mov|ass|json|txt)$")


def _groups(root: Path) -> dict[Path, dict]:
    groups: dict[Path, dict] = {}
    for dirpath, _, files in os.walk(root):
        for fn in files:
            m = _STAMP.match(fn)
            if not m:
                continue
            path = Path(dirpath) / fn
            try:
                st = path.stat()
            except OSError:
                continue
            g = groups.setdefault(Path(dirpath) / m.group(1), {"paths": [], "size": 0, "mtime": 0.0})
            g["paths"].append(path)
            g["size"] += st.st_size
            g["mtime"] = max(g["mtime"], st.st_mtime)
    return groups


def usage(root: Path) -> tuple[int, int]:
    """(bytes, clip count) under root."""
    g = _groups(Path(root))
    return sum(x["size"] for x in g.values()), len(g)


def enforce(root: Path, max_gb: float = 0, max_clips: int = 0) -> tuple[int, int]:
    """Delete oldest clip groups until under both caps (0 disables a cap).
    Returns (bytes_freed, clips_deleted). Always keeps at least one clip."""
    root = Path(root)
    if not root.is_dir() or (max_gb <= 0 and max_clips <= 0):
        return 0, 0
    groups = sorted(_groups(root).items(), key=lambda kv: kv[1]["mtime"])
    total = sum(g["size"] for _, g in groups)
    count = len(groups)
    max_bytes = max_gb * 1024 ** 3 if max_gb > 0 else None
    freed = deleted = 0
    for stem, g in groups:
        over = (max_bytes is not None and total > max_bytes) or (max_clips > 0 and count > max_clips)
        if not over or count <= 1:
            break
        for p in g["paths"]:
            try:
                p.unlink()
            except OSError as e:
                log.debug("could not remove %s: %s", p, e)
        total -= g["size"]
        count -= 1
        freed += g["size"]
        deleted += 1
        log.info("retention: pruned %s (%.0f MB)", stem.name, g["size"] / 1e6)
    return freed, deleted

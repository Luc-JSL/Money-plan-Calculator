"""Append-only JSONL log of every clip the bot makes, plus a rolling rate limiter."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ClipRecord:
    clip_id: str
    url: str
    channel: str
    broadcaster_id: str
    game: str
    created_at: str
    reason: str
    score: float
    short_rate: float
    baseline_rate: float
    unique_users: int
    hype_fraction: float
    sample: list[str] = field(default_factory=list)
    file: str = ""
    vertical_file: str = ""
    title: str = ""
    duration: float = 0.0
    stream_title: str = ""
    viewers: int = 0
    layout: str = ""
    posted: bool = False
    post_urls: list[str] = field(default_factory=list)


class ClipLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, rec: ClipRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def read(self) -> list[ClipRecord]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(ClipRecord(**json.loads(line)))
                except (ValueError, TypeError):
                    continue
        return out


class RateLimiter:
    """Allow at most `limit` events per `period` seconds (sliding window)."""

    def __init__(self, limit: int, period: float = 3600.0, clock=time.monotonic):
        self.limit, self.period, self.clock = limit, period, clock
        self.events: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self.events and self.events[0] <= now - self.period:
            self.events.popleft()
        if len(self.events) >= self.limit:
            return False
        self.events.append(now)
        return True

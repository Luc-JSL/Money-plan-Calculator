"""Chat spike detector: decides when a stream just had a "moment".

Signal: message rate in a short window vs. the baseline rate over a longer
window, weighted by how many of the burst messages look like hype (emotes,
LUL/KEKW/POG style words, caps, "clip it"). Pure logic, injectable clock,
fully unit-tested.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

from .config import DetectorConfig

HYPE_WORDS = {
    "lul", "lol", "lmao", "lmfao", "lulw", "kekw", "omegalul", "icant", "pog", "pogchamp", "poggers",
    "pogu", "pogg", "wtf", "omg", "omfg", "wow", "insane", "crazy", "holy", "bruh", "gg", "ez", "clip",
    "clipped", "clipit", "w", "l", "xd", "sheesh", "nah", "noway", "cinema", "peak", "goat", "rip", "f",
    "lets", "letsgo", "letsgoo", "gigachad", "chad", "cooked", "lmaooo", "hahaha", "haha", "jesus",
    "dead", "dying", "crying", "screaming", "ayo", "yo", "what", "how", "aura", "wild",
}
HYPE_PHRASES = ("no way", "clip it", "clip that", "clip this", "oh my god", "what the", "let's go",
                "lets go", "hell nah", "he's cooked", "hes cooked", "im dead", "i'm dead", "that's crazy",
                "thats crazy", "lmfao", "holy shit", "so real", "no shot")
_WORD_RE = re.compile(r"[a-z0-9']+")
_REPEAT_RE = re.compile(r"(.)\1{3,}")        # aaaaa, !!!!, ?????
_CLIP_RE = re.compile(r"\bclip(?:ped|s|it)?\b")


def is_hype(text: str, emote_count: int = 0) -> bool:
    t = text.strip()
    if not t:
        return False
    if emote_count > 0:
        return True
    low = t.lower()
    if any(p in low for p in HYPE_PHRASES):
        return True
    words = _WORD_RE.findall(low)
    if any(w in HYPE_WORDS for w in words):
        return True
    if _REPEAT_RE.search(low):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) >= 0.7:
        return True
    if low.count("?") >= 3 or low.count("!") >= 3:
        return True
    return False


def is_clip_request(text: str) -> bool:
    return bool(_CLIP_RE.search(text.lower()))


@dataclass
class Trigger:
    channel: str
    ts: float
    reason: str              # "spike" or "clip_requests"
    score: float             # higher = more intense
    short_rate: float
    baseline_rate: float
    unique_users: int
    hype_fraction: float
    sample: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.reason} score={self.score:.1f} rate={self.short_rate:.1f}/s "
                f"baseline={self.baseline_rate:.2f}/s x{self.short_rate / max(self.baseline_rate, 1e-9):.1f} "
                f"users={self.unique_users} hype={self.hype_fraction:.0%}")


@dataclass
class _Event:
    ts: float
    user: str
    text: str
    hype: bool
    clip_req: bool


class ChannelState:
    def __init__(self, first_seen: float):
        self.first_seen = first_seen
        self.events: deque[_Event] = deque()
        self.last_event = first_seen
        self.last_trigger = -1e18
        self.triggers = 0


class SpikeDetector:
    def __init__(self, cfg: DetectorConfig | None = None, clock=time.monotonic):
        self.cfg = cfg or DetectorConfig()
        self.clock = clock
        self.channels: dict[str, ChannelState] = {}
        self.max_user_share = 0.5

    def forget(self, channel: str) -> None:
        self.channels.pop(channel, None)

    def observe(self, channel: str, user: str, text: str, emote_count: int = 0,
                ts: float | None = None) -> Trigger | None:
        """Feed one chat message. Returns a Trigger if this message tips the channel over."""
        cfg = self.cfg
        now = self.clock() if ts is None else ts
        st = self.channels.get(channel)
        if st is None:
            st = self.channels[channel] = ChannelState(first_seen=now)
        if cfg.warmup > 0 and now - st.last_event > cfg.warmup:
            # Long silence (IRC reconnect, stream restart): the baseline is stale, re-warm.
            st.first_seen = now
            st.events.clear()
        st.last_event = now
        st.events.append(_Event(now, user.lower(), text, is_hype(text, emote_count), is_clip_request(text)))
        cutoff = now - cfg.long_window
        while st.events and st.events[0].ts < cutoff:
            st.events.popleft()

        if now - st.first_seen < cfg.warmup:
            return None
        if now - st.last_trigger < cfg.cooldown:
            return None

        short_cut = now - cfg.short_window
        short = [e for e in st.events if e.ts >= short_cut]
        long_count = len(st.events) - len(short)
        observed_long = min(now - st.first_seen, cfg.long_window) - cfg.short_window
        baseline = long_count / observed_long if observed_long > 0 else 0.0
        baseline = max(baseline, cfg.min_baseline_rate)
        per_user: dict[str, int] = {}
        for e in short:
            per_user[e.user] = per_user.get(e.user, 0) + 1
        short_rate = len(short) / cfg.short_window
        users = set(per_user)
        # One spammer must not be able to fake a burst: the loudest user may own at most half of it.
        top_share = max(per_user.values()) / len(short) if short else 0.0
        hype_frac = sum(e.hype for e in short) / len(short) if short else 0.0
        clip_users = {e.user for e in short if e.clip_req}

        reason = None
        if top_share > self.max_user_share:
            return None
        if len(clip_users) >= cfg.clip_request_users:
            reason = "clip_requests"
        elif (short_rate >= cfg.min_short_rate and short_rate >= baseline * cfg.spike_multiplier
              and len(users) >= cfg.min_unique_users and hype_frac >= cfg.min_hype_fraction):
            reason = "spike"
        if reason is None:
            return None

        ratio = short_rate / baseline
        score = ratio * (0.5 + hype_frac) + 2.0 * len(clip_users)
        st.last_trigger = now
        st.triggers += 1
        sample = [e.text for e in short[-8:]]
        return Trigger(channel=channel, ts=now, reason=reason, score=round(score, 2), short_rate=short_rate,
                       baseline_rate=baseline, unique_users=len(users), hype_fraction=hype_frac, sample=sample)

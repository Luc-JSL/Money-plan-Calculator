"""Orchestrator: stream discovery + chat monitoring + clipping."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .auth import AuthError, TokenManager, TokenStore
from .config import Config
from .detector import SpikeDetector, Trigger
from .irc import ChatPool
from .media import download_clip, render_vertical, safe_name
from .notify import discord
from .storage import ClipLog, ClipRecord, RateLimiter
from .twitch_api import Helix, HelixError, Stream

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: Config, helix: Helix | None = None):
        self.cfg = cfg
        self.helix = helix or Helix(cfg.client_id, TokenManager(cfg.client_id, TokenStore(cfg.token_path),
                                                                 cfg.client_secret))
        self.detector = SpikeDetector(cfg.detector)
        self.pool = ChatPool()
        self.log = ClipLog(cfg.log_path)
        self.limiter = RateLimiter(cfg.max_clips_per_hour)
        self.streams: dict[str, Stream] = {}       # login -> live stream info
        self.game_id = cfg.game_id
        self.in_flight: set[str] = set()
        self.blocked_until: dict[str, float] = {}  # login -> monotonic time (clips disabled etc.)
        self._stop = asyncio.Event()

    # ----- discovery ------------------------------------------------------
    async def resolve_game(self) -> None:
        if self.game_id or not self.cfg.game_name or self.cfg.top_streams == 0:
            return
        gid = await asyncio.to_thread(self.helix.get_game_id, self.cfg.game_name)
        if not gid:
            log.warning("game %r not found on Twitch yet; watching only CHANNELS until it appears",
                        self.cfg.game_name)
            return
        self.game_id = gid
        log.info("game %r -> id %s", self.cfg.game_name, gid)

    def pick_streams(self, top: list[Stream], pinned: list[Stream]) -> dict[str, Stream]:
        chosen: dict[str, Stream] = {s.login: s for s in pinned}
        for s in sorted(top, key=lambda s: -s.viewer_count):
            if len(chosen) >= self.cfg.top_streams + len(pinned):
                break
            chosen.setdefault(s.login, s)
        return chosen

    async def refresh_streams(self) -> None:
        if not self.game_id:
            await self.resolve_game()
        top: list[Stream] = []
        pinned: list[Stream] = []
        try:
            if self.game_id and self.cfg.top_streams > 0:
                top = await asyncio.to_thread(self.helix.get_streams, game_id=self.game_id,
                                              languages=self.cfg.languages or None, first=100)
            if self.cfg.channels:
                pinned = await asyncio.to_thread(self.helix.get_streams, logins=self.cfg.channels, first=100)
        except (HelixError, AuthError) as e:
            log.error("stream refresh failed: %s", e)
            return
        chosen = self.pick_streams(top, pinned)
        gone = set(self.streams) - set(chosen)
        for login in gone:
            self.detector.forget(login)
        self.streams = chosen
        await self.pool.set_channels(set(chosen))
        if chosen:
            top3 = ", ".join(f"{s.login}({s.viewer_count})" for s in
                             sorted(chosen.values(), key=lambda s: -s.viewer_count)[:3])
            log.info("watching %d streams; top: %s", len(chosen), top3)
        else:
            log.info("no live streams to watch right now")

    async def discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh_streams()
            except Exception as e:  # noqa: BLE001
                log.exception("discovery error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.refresh_interval)
            except asyncio.TimeoutError:
                pass

    # ----- chat -> trigger ------------------------------------------------
    async def chat_loop(self) -> None:
        async for m in self.pool.messages():
            if m.channel not in self.streams:
                continue
            trig = self.detector.observe(m.channel, m.user, m.text, m.emote_count)
            if trig:
                asyncio.create_task(self.handle_trigger(trig))

    async def handle_trigger(self, trig: Trigger) -> None:
        login = trig.channel
        now = time.monotonic()
        if login in self.in_flight or self.blocked_until.get(login, 0) > now:
            return
        if not self.limiter.allow():
            log.warning("MAX_CLIPS_PER_HOUR reached; skipping %s (%s)", login, trig.summary())
            return
        stream = self.streams.get(login)
        if not stream:
            return
        self.in_flight.add(login)
        try:
            log.info("TRIGGER %s: %s | sample: %s", login, trig.summary(), " | ".join(trig.sample[-3:]))
            await asyncio.to_thread(self.make_clip, stream, trig)
        except HelixError as e:
            if e.status in (403, 404):
                # clips disabled / followers-only clipping / channel gone: back off an hour
                self.blocked_until[login] = now + 3600
                log.warning("cannot clip %s (%s); ignoring channel for 1h", login, e)
            else:
                log.error("clip failed for %s: %s", login, e)
        except AuthError as e:
            log.error("auth problem: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected error clipping %s: %s", login, e)
        finally:
            self.in_flight.discard(login)

    # ----- clip pipeline (runs in a worker thread) -------------------------
    def make_clip(self, stream: Stream, trig: Trigger) -> ClipRecord | None:
        clip_id, _edit = self.helix.create_clip(stream.user_id, self.cfg.has_delay)
        clip = self.helix.wait_for_clip(clip_id)
        url = clip.url if clip else f"https://clips.twitch.tv/{clip_id}"
        rec = ClipRecord(
            clip_id=clip_id, url=url, channel=stream.login, broadcaster_id=stream.user_id,
            game=stream.game_name, created_at=clip.created_at if clip else "", reason=trig.reason,
            score=trig.score, short_rate=round(trig.short_rate, 2), baseline_rate=round(trig.baseline_rate, 3),
            unique_users=trig.unique_users, hype_fraction=round(trig.hype_fraction, 2), sample=trig.sample,
            title=clip.title if clip else "", duration=clip.duration if clip else 0.0,
            stream_title=stream.title, viewers=stream.viewer_count,
        )
        log.info("CLIP %s -> %s", stream.login, url)
        if clip is None:
            log.warning("clip %s not visible via Get Clips yet; download skipped", clip_id)
        elif self.cfg.download:
            base = self.cfg.clips_dir / safe_name(stream.login)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = download_clip(url, base / f"{stamp}-{safe_name(clip_id)}.mp4")
            if path:
                rec.file = str(path)
                log.info("downloaded %s", path)
                if self.cfg.vertical:
                    vpath = render_vertical(path, path.with_name(path.stem + "-vertical.mp4"))
                    if vpath:
                        rec.vertical_file = str(vpath)
                        log.info("rendered vertical %s", vpath)
        self.log.append(rec)
        if self.cfg.discord_webhook:
            discord(self.cfg.discord_webhook,
                    f"**{stream.display_name or stream.login}** ({stream.viewer_count:,} viewers) "
                    f"- {trig.summary()}\n{url}")
        return rec

    # ----- entrypoints ----------------------------------------------------
    async def run(self) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        tasks = [asyncio.create_task(self.discovery_loop(), name="discovery"),
                 asyncio.create_task(self.chat_loop(), name="chat")]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await self.pool.close()

    def stop(self) -> None:
        self._stop.set()

    def clip_now(self, login: str, reason: str = "manual") -> ClipRecord | None:
        """Create a clip of `login` right now (used by the CLI)."""
        streams = self.helix.get_streams(logins=[login])
        if not streams:
            raise RuntimeError(f"{login} is not live")
        trig = Trigger(channel=login, ts=time.monotonic(), reason=reason, score=0, short_rate=0,
                       baseline_rate=0, unique_users=0, hype_fraction=0)
        return self.make_clip(streams[0], trig)

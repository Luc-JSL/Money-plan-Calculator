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
from . import captions, retention
from .post import Poster
from .media import download_clip, render_vertical, safe_name
from .recorder import Recorder
from .notify import discord
from .storage import ClipLog, ClipRecord, RateLimiter
from .twitch_api import Helix, HelixError, Stream

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: Config, helix: Helix | None = None):
        self.cfg = cfg
        self.helix: Helix | None = helix
        if self.helix is None and (cfg.has_twitch_auth() or not cfg.local_mode):
            self.helix = Helix(cfg.client_id, TokenManager(cfg.client_id, TokenStore(cfg.token_path),
                                                           cfg.client_secret))
        self.recorders: dict[str, Recorder] = {}
        self.poster = Poster(cfg) if cfg.auto_post else None
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
        if self.game_id or not self.cfg.game_name or self.cfg.top_streams == 0 or self.helix is None:
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

    def _offline_stream(self, login: str) -> Stream:
        """Placeholder for a pinned channel when no Twitch API is available."""
        return Stream(user_id="", login=login, display_name=login, game_id="", game_name=self.cfg.game_name,
                      title="", viewer_count=0, language="")

    async def refresh_streams(self) -> None:
        if not self.game_id:
            await self.resolve_game()
        top: list[Stream] = []
        pinned: list[Stream] = []
        if self.helix is None:
            # No Twitch app/token: watch CHANNELS; streamlink finds out whether they are live.
            pinned = [self._offline_stream(c) for c in self.cfg.channels]
        else:
            try:
                if self.game_id and self.cfg.top_streams > 0:
                    top = await asyncio.to_thread(self.helix.get_streams, game_id=self.game_id,
                                                  languages=self.cfg.languages or None, first=100)
                if self.cfg.channels:
                    pinned = await asyncio.to_thread(self.helix.get_streams, logins=self.cfg.channels, first=100)
            except (HelixError, AuthError) as e:
                log.error("stream refresh failed: %s", e)
                if not self.cfg.local_mode:
                    return
                pinned = [self._offline_stream(c) for c in self.cfg.channels]
        chosen = self.pick_streams(top, pinned)
        gone = set(self.streams) - set(chosen)
        for login in gone:
            self.detector.forget(login)
        self.streams = chosen
        await self.pool.set_channels(set(chosen))
        if self.cfg.local_mode:
            await self._sync_recorders(set(chosen))
        if chosen and self.helix is None:
            log.info("watching channels: %s", ", ".join(sorted(chosen)))
        elif chosen:
            top3 = ", ".join(f"{s.login}({s.viewer_count})" for s in
                             sorted(chosen.values(), key=lambda s: -s.viewer_count)[:3])
            log.info("watching %d streams; top: %s", len(chosen), top3)
        else:
            log.info("no live streams to watch right now")

    async def _sync_recorders(self, wanted: set[str]) -> None:
        for login in set(self.recorders) - wanted:
            rec = self.recorders.pop(login)
            await rec.stop()
            log.info("[%s] recording stopped", login)
        for login in wanted - set(self.recorders):
            rec = Recorder(login, self.cfg.buffer_dir / safe_name(login), self.cfg.buffer_seconds, self.cfg.quality)
            self.recorders[login] = rec
            await rec.start()

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
            if m.kind == "raid":
                log.info("raid into %s (%s); ignoring chat for %.0fs", m.channel, m.text or m.user,
                         self.cfg.detector.raid_suppress)
                self.detector.suppress(m.channel, self.cfg.detector.raid_suppress)
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
            if self.cfg.local_mode:
                await self.make_clip_local(stream, trig)
            else:
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

    # ----- local clip pipeline (streamlink buffer, no token) ---------------
    async def make_clip_local(self, stream: Stream, trig: Trigger, trigger_time: float | None = None) -> ClipRecord | None:
        rec = self.recorders.get(stream.login)
        if rec is None:
            log.warning("no recorder for %s", stream.login)
            return None
        if not rec.is_live():
            log.info("%s has no buffered video (offline or still starting); skipping", stream.login)
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        clip_id = f"{stamp}-local"
        dest = self.cfg.clips_dir / safe_name(stream.login) / f"{stamp}-local.mp4"
        path = await rec.cut(trigger_time or time.time(), self.cfg.clip_pre_seconds, self.cfg.clip_post_seconds, dest)
        if path is None:
            return None
        record = ClipRecord(
            clip_id=clip_id, url="", channel=stream.login, broadcaster_id=stream.user_id, game=stream.game_name,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), reason=trig.reason, score=trig.score,
            short_rate=round(trig.short_rate, 2), baseline_rate=round(trig.baseline_rate, 3),
            unique_users=trig.unique_users, hype_fraction=round(trig.hype_fraction, 2), sample=trig.sample,
            file=str(path), stream_title=stream.title, viewers=stream.viewer_count,
            duration=self.cfg.clip_pre_seconds + self.cfg.clip_post_seconds,
        )
        log.info("CLIP %s -> %s", stream.login, path)
        await asyncio.to_thread(self._finish, record, path, stream, trig)
        return record

    def _finish(self, record: ClipRecord, path: Path, stream: Stream, trig: Trigger) -> None:
        """Captions, vertical render, optional posting, retention, log, notify."""
        if self.cfg.vertical:
            ass = None
            if self.cfg.captions:
                ass = captions.make_captions(path, path.with_suffix(".ass"), model_size=self.cfg.whisper_model)
            vpath = render_vertical(path, path.with_name(path.stem + "-vertical.mp4"), ass_path=ass)
            if vpath:
                record.vertical_file = str(vpath)
                log.info("rendered vertical %s%s", vpath, " with captions" if ass else "")
                if self.poster is not None:
                    text, title = self.cfg.render_post_text(stream.display_name, stream.login, stream.game_name,
                                                            stream.title, record.url)
                    results = self.poster.post_all(vpath, text, title)
                    record.posted = any(r.ok for r in results)
                    record.post_urls = [r.url for r in results if r.ok and r.url]
        retention.enforce(self.cfg.clips_dir, self.cfg.max_clips_gb, self.cfg.max_clips_keep)
        self.log.append(record)
        if self.cfg.discord_webhook:
            link = record.url or Path(record.vertical_file or record.file).name
            viewers = f" ({stream.viewer_count:,} viewers)" if stream.viewer_count else ""
            discord(self.cfg.discord_webhook,
                    f"**{stream.display_name or stream.login}**{viewers} - {trig.summary()}\n{link}")

    # ----- Twitch API clip pipeline (runs in a worker thread) --------------
    def make_clip(self, stream: Stream, trig: Trigger) -> ClipRecord | None:
        assert self.helix is not None, "twitch mode needs a Helix client"
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
                self._finish(rec, path, stream, trig)
                return rec
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
            for rec in self.recorders.values():
                await rec.stop()

    def stop(self) -> None:
        self._stop.set()

    def clip_now(self, login: str, reason: str = "manual") -> ClipRecord | None:
        """Create a clip of `login` right now via the Twitch API (used by the CLI)."""
        if self.helix is None:
            raise RuntimeError("manual clipping needs a Twitch app + `auth` (CLIP_MODE=twitch), "
                               "or run the bot in local mode and let chat trigger clips")
        streams = self.helix.get_streams(logins=[login])
        if not streams:
            raise RuntimeError(f"{login} is not live")
        trig = Trigger(channel=login, ts=time.monotonic(), reason=reason, score=0, short_rate=0,
                       baseline_rate=0, unique_users=0, hype_fraction=0)
        return self.make_clip(streams[0], trig)

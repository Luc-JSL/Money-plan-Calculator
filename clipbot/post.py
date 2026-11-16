"""Posting clips to TikTok and YouTube Shorts, with a daily cap and a queue.

- YouTube: official Data API (see youtube.py). Safe. ~6 uploads/day on a new
  Google project because of API quota.
- TikTok: the MIT-licensed `tiktok-uploader` package, which drives a
  Playwright browser with cookies exported from tiktok.com. TikTok's terms do
  not allow this; accounts can be rate-limited or banned. Use a dedicated clips
  account and keep volume human.

`PostState` (data/post_state.json) remembers what was posted and how many
posts went out today per platform, so `clipbot post --pending` can drain the
queue later without double-posting.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PLATFORMS = ("tiktok", "youtube")


def tiktok_available() -> bool:
    try:
        import tiktok_uploader  # noqa: F401
        return True
    except ImportError:
        return False


available = tiktok_available  # backwards compatibility


def post_tiktok(video: Path, description: str, cookies: str, headless: bool = True, uploader=None) -> bool:
    """Upload one video to TikTok. Returns True on success. `uploader` is injectable for tests."""
    video = Path(video)
    if not video.is_file():
        log.error("tiktok: %s does not exist", video)
        return False
    if not cookies or not Path(cookies).is_file():
        log.error("tiktok: TIKTOK_COOKIES file not found: %r", cookies)
        return False
    if uploader is None:
        if not tiktok_available():
            log.error("tiktok: tiktok-uploader not installed (pip install tiktok-uploader; playwright install chromium)")
            return False
        from tiktok_uploader.upload import upload_video  # type: ignore
        uploader = upload_video
    try:
        failed = uploader(str(video), description=description, cookies=cookies, headless=headless, browser="chromium")
    except Exception as e:  # noqa: BLE001
        log.error("tiktok: upload failed for %s: %s", video, e)
        return False
    if failed:
        log.error("tiktok: rejected %s: %s", video, failed)
        return False
    log.info("tiktok: uploaded %s", video.name)
    return True


class PostState:
    """Persistent record of posted files and per-day counts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"posted": {}, "days": {}}
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        self.data.setdefault("posted", {})
        self.data.setdefault("days", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def count_today(self, platform: str) -> int:
        return int(self.data["days"].get(self._today(), {}).get(platform, 0))

    def is_posted(self, video: Path, platform: str) -> bool:
        return platform in self.data["posted"].get(str(video), {})

    def record(self, video: Path, platform: str, url: str = "") -> None:
        self.data["posted"].setdefault(str(video), {})[platform] = url or "ok"
        day = self.data["days"].setdefault(self._today(), {})
        day[platform] = int(day.get(platform, 0)) + 1
        # keep only the last 30 days of counters
        for d in sorted(self.data["days"])[:-30]:
            self.data["days"].pop(d, None)
        self._save()


@dataclass
class PostResult:
    platform: str
    ok: bool
    url: str = ""
    skipped: str = ""      # why it was not attempted ("cap", "already", ...)


class Poster:
    """Posts a vertical clip to the configured platforms, honouring the daily cap."""

    def __init__(self, cfg, state: PostState | None = None, youtube=None, tiktok_uploader=None):
        self.cfg = cfg
        self.state = state or PostState(cfg.data_dir / "post_state.json")
        self._yt = youtube
        self._tiktok_uploader = tiktok_uploader

    def _youtube(self):
        if self._yt is None:
            from .youtube import GoogleTokenStore, YouTube
            self._yt = YouTube(self.cfg.youtube_client_id, self.cfg.youtube_client_secret,
                               GoogleTokenStore(self.cfg.data_dir / "youtube_tokens.json"))
        return self._yt

    def post(self, video: Path, platform: str, text: str, title: str = "", force: bool = False) -> PostResult:
        video = Path(video)
        if self.state.is_posted(video, platform):
            return PostResult(platform, False, skipped="already")
        if not force and self.state.count_today(platform) >= self.cfg.max_posts_per_day:
            log.info("%s: daily cap (%d) reached; %s queued for `clipbot post --pending`",
                     platform, self.cfg.max_posts_per_day, video.name)
            return PostResult(platform, False, skipped="cap")
        try:
            if platform == "youtube":
                url = self._youtube().upload(video, title or text[:95], text, tags=self.cfg.youtube_tags,
                                             privacy=self.cfg.youtube_privacy)
                ok = True
            elif platform == "tiktok":
                ok = post_tiktok(video, text, self.cfg.tiktok_cookies, uploader=self._tiktok_uploader)
                url = ""
            else:
                return PostResult(platform, False, skipped=f"unknown platform {platform}")
        except Exception as e:  # noqa: BLE001
            log.error("%s: posting %s failed: %s", platform, video.name, e)
            return PostResult(platform, False)
        if ok:
            self.state.record(video, platform, url)
        return PostResult(platform, ok, url)

    def post_all(self, video: Path, text: str, title: str = "") -> list[PostResult]:
        return [self.post(video, p, text, title) for p in self.cfg.auto_post]

    def post_pending(self, records, limit: int | None = None) -> list[PostResult]:
        """Post the best unposted vertical clips from `records` (ClipRecord list), highest score first."""
        out: list[PostResult] = []
        candidates = [r for r in records if r.vertical_file and Path(r.vertical_file).is_file()]
        candidates.sort(key=lambda r: -r.score)
        for r in candidates:
            text, title = self.cfg.render_post_text(r.channel, r.channel, r.game, r.stream_title, r.url)
            for p in self.cfg.auto_post or list(PLATFORMS):
                if p == "tiktok" and not self.cfg.tiktok_cookies:
                    continue
                if p == "youtube" and not self.cfg.youtube_client_id:
                    continue
                if self.state.is_posted(Path(r.vertical_file), p):
                    continue
                res = self.post(Path(r.vertical_file), p, text, title)
                if res.skipped == "cap":
                    continue
                out.append(res)
                if limit and len(out) >= limit:
                    return out
        return out

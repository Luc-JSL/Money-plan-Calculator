"""Configuration loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no interpolation.

    Values already present in the environment are never overwritten.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _env_float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _parse_platforms(v: str) -> list[str]:
    """AUTO_POST accepts 'youtube,tiktok', 'true' (= tiktok, legacy) or empty/false."""
    v = v.strip().lower()
    if not v or v in {"0", "false", "no", "off"}:
        return []
    if v in {"1", "true", "yes", "on"}:
        return ["tiktok"]
    return [x.strip() for x in v.split(",") if x.strip()]


def _env_list(name: str) -> list[str]:
    v = _env(name)
    return [x.strip().lstrip("#").lower() for x in v.split(",") if x.strip()]


@dataclass
class DetectorConfig:
    """Tunables for the chat spike detector. Times are seconds, rates are msgs/sec."""

    short_window: float = 10.0        # window in which a burst must happen
    long_window: float = 120.0        # baseline window
    warmup: float = 60.0              # observe a channel this long before triggering
    spike_multiplier: float = 3.0     # short-window rate must be >= baseline * this
    min_short_rate: float = 1.0       # and at least this many msgs/sec in the burst
    min_baseline_rate: float = 0.25   # floor so tiny chats don't produce huge ratios
    min_unique_users: int = 5         # distinct chatters in the burst
    min_hype_fraction: float = 0.35   # fraction of burst messages that look like hype
    clip_request_users: int = 3       # distinct users typing "clip" -> trigger
    cooldown: float = 120.0           # per-channel seconds between clips
    sustain: float = 3.0              # spike must hold this long (0 = fire instantly)
    raid_suppress: float = 120.0      # ignore a channel this long after an incoming raid

    @classmethod
    def from_env(cls) -> "DetectorConfig":
        d = cls()
        return cls(
            short_window=_env_float("DET_SHORT_WINDOW", d.short_window),
            long_window=_env_float("DET_LONG_WINDOW", d.long_window),
            warmup=_env_float("DET_WARMUP", d.warmup),
            spike_multiplier=_env_float("DET_SPIKE_MULTIPLIER", d.spike_multiplier),
            min_short_rate=_env_float("DET_MIN_SHORT_RATE", d.min_short_rate),
            min_baseline_rate=_env_float("DET_MIN_BASELINE_RATE", d.min_baseline_rate),
            min_unique_users=_env_int("DET_MIN_UNIQUE_USERS", d.min_unique_users),
            min_hype_fraction=_env_float("DET_MIN_HYPE_FRACTION", d.min_hype_fraction),
            clip_request_users=_env_int("DET_CLIP_REQUEST_USERS", d.clip_request_users),
            cooldown=_env_float("DET_COOLDOWN", d.cooldown),
            sustain=_env_float("DET_SUSTAIN", d.sustain),
            raid_suppress=_env_float("DET_RAID_SUPPRESS", d.raid_suppress),
        )


@dataclass
class Config:
    client_id: str = ""
    client_secret: str = ""          # optional; only for "confidential" Twitch apps
    game_name: str = "Grand Theft Auto VI"
    game_id: str = ""                # optional override; skips the name lookup
    channels: list[str] = field(default_factory=list)   # always-monitored channels
    top_streams: int = 15            # how many top live streams of the game to watch
    languages: list[str] = field(default_factory=list)  # e.g. ["en"]; empty = all
    refresh_interval: float = 120.0  # seconds between stream-list refreshes
    clip_mode: str = "local"         # "local": streamlink buffer + ffmpeg (no token); "twitch": Create Clip API
    clip_pre_seconds: float = 30.0   # local mode: seconds before the trigger to keep
    clip_post_seconds: float = 0.0   # local mode: seconds after the trigger to keep
    buffer_seconds: float = 180.0    # local mode: rolling buffer per channel
    quality: str = "720p60,720p,480p,best"  # local mode: streamlink quality preference
    has_delay: bool = False          # twitch mode: Twitch "has_delay" when creating clips
    download: bool = True
    vertical: bool = True
    captions: bool = True            # burn auto-captions into the vertical clip (needs faster-whisper)
    whisper_model: str = "small"     # faster-whisper model size: tiny/base/small/medium
    max_clips_gb: float = 5.0        # prune oldest clips past this size (0 = off)
    max_clips_keep: int = 300        # or past this many clips (0 = off)
    auto_post: list[str] = field(default_factory=list)  # platforms: "youtube", "tiktok" (opt-in, see README)
    max_posts_per_day: int = 6       # per platform; YouTube quota allows ~6, TikTok should look human
    tiktok_cookies: str = ""         # path to a Netscape cookies.txt exported from tiktok.com
    post_template: str = "{display_name} on Twitch #twitch #clips #gta6 #gtavi"
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_privacy: str = "public"  # public | unlisted | private
    youtube_title_template: str = "{display_name} {game} moment #shorts"
    youtube_tags: list[str] = field(default_factory=lambda: ["twitch", "clips", "gta6", "gtavi", "shorts"])
    data_dir: Path = Path("data")
    discord_webhook: str = ""
    max_clips_per_hour: int = 30     # global safety valve
    log_level: str = "INFO"
    detector: DetectorConfig = field(default_factory=DetectorConfig)

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        return cls(
            client_id=_env("TWITCH_CLIENT_ID"),
            client_secret=_env("TWITCH_CLIENT_SECRET"),
            game_name=_env("GAME_NAME", d.game_name),
            game_id=_env("GAME_ID"),
            channels=_env_list("CHANNELS"),
            top_streams=_env_int("TOP_STREAMS", d.top_streams),
            languages=_env_list("LANGUAGES"),
            refresh_interval=_env_float("REFRESH_INTERVAL", d.refresh_interval),
            clip_mode=_env("CLIP_MODE", d.clip_mode).lower(),
            clip_pre_seconds=_env_float("CLIP_PRE_SECONDS", d.clip_pre_seconds),
            clip_post_seconds=_env_float("CLIP_POST_SECONDS", d.clip_post_seconds),
            buffer_seconds=_env_float("BUFFER_SECONDS", d.buffer_seconds),
            quality=_env("QUALITY", d.quality),
            has_delay=_env_bool("HAS_DELAY", d.has_delay),
            download=_env_bool("DOWNLOAD", d.download),
            vertical=_env_bool("VERTICAL", d.vertical),
            captions=_env_bool("CAPTIONS", d.captions),
            whisper_model=_env("WHISPER_MODEL", d.whisper_model),
            max_clips_gb=_env_float("MAX_CLIPS_GB", d.max_clips_gb),
            max_clips_keep=_env_int("MAX_CLIPS_KEEP", d.max_clips_keep),
            auto_post=_parse_platforms(_env("AUTO_POST")),
            max_posts_per_day=_env_int("MAX_POSTS_PER_DAY", d.max_posts_per_day),
            tiktok_cookies=_env("TIKTOK_COOKIES"),
            post_template=_env("POST_TEMPLATE", d.post_template),
            youtube_client_id=_env("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_env("YOUTUBE_CLIENT_SECRET"),
            youtube_privacy=_env("YOUTUBE_PRIVACY", d.youtube_privacy).lower(),
            youtube_title_template=_env("YOUTUBE_TITLE_TEMPLATE", d.youtube_title_template),
            youtube_tags=_env_list("YOUTUBE_TAGS") or d.youtube_tags,
            data_dir=Path(_env("DATA_DIR", str(d.data_dir))),
            discord_webhook=_env("DISCORD_WEBHOOK"),
            max_clips_per_hour=_env_int("MAX_CLIPS_PER_HOUR", d.max_clips_per_hour),
            log_level=_env("LOG_LEVEL", d.log_level).upper(),
            detector=DetectorConfig.from_env(),
        )

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"

    @property
    def local_mode(self) -> bool:
        return self.clip_mode == "local"

    def has_twitch_auth(self) -> bool:
        return bool(self.client_id) and self.token_path.is_file()

    def render_post_text(self, display_name: str, login: str, game: str, title: str, url: str) -> tuple[str, str]:
        """(caption/description, youtube title) from the templates."""
        vals = dict(display_name=display_name or login, login=login, game=game, title=title, url=url)
        return self.post_template.format(**vals), self.youtube_title_template.format(**vals)

    @property
    def log_path(self) -> Path:
        return self.data_dir / "clips.jsonl"

    def validate(self) -> list[str]:
        problems = []
        if self.clip_mode not in ("local", "twitch"):
            problems.append("CLIP_MODE must be 'local' or 'twitch'")
        if self.clip_mode == "twitch" and not self.client_id:
            problems.append("CLIP_MODE=twitch needs TWITCH_CLIENT_ID")
        if self.top_streams < 0 or self.top_streams > 100:
            problems.append("TOP_STREAMS must be between 0 and 100")
        if self.top_streams == 0 and not self.channels:
            problems.append("Nothing to watch: set TOP_STREAMS > 0 or CHANNELS")
        if self.local_mode and not self.channels and not self.client_id:
            problems.append("Local mode without a Twitch app needs CHANNELS (top-stream discovery needs TWITCH_CLIENT_ID)")
        if self.local_mode and self.clip_pre_seconds + self.clip_post_seconds > self.buffer_seconds:
            problems.append("CLIP_PRE_SECONDS + CLIP_POST_SECONDS must fit inside BUFFER_SECONDS")
        if self.local_mode and self.clip_pre_seconds <= 0:
            problems.append("CLIP_PRE_SECONDS must be > 0")
        if self.detector.short_window >= self.detector.long_window:
            problems.append("DET_SHORT_WINDOW must be smaller than DET_LONG_WINDOW")
        for p in self.auto_post:
            if p not in ("tiktok", "youtube"):
                problems.append(f"AUTO_POST: unknown platform {p!r} (use youtube, tiktok)")
        if "tiktok" in self.auto_post and not self.tiktok_cookies:
            problems.append("AUTO_POST=tiktok needs TIKTOK_COOKIES (path to cookies.txt)")
        if "youtube" in self.auto_post and not (self.youtube_client_id and self.youtube_client_secret):
            problems.append("AUTO_POST=youtube needs YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET")
        if self.auto_post and not self.vertical:
            problems.append("AUTO_POST needs VERTICAL=true")
        if self.auto_post and not self.local_mode and not self.download:
            problems.append("AUTO_POST in twitch mode needs DOWNLOAD=true")
        if self.youtube_privacy not in ("public", "unlisted", "private"):
            problems.append("YOUTUBE_PRIVACY must be public, unlisted or private")
        return problems

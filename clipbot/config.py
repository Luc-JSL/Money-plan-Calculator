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
    has_delay: bool = False          # Twitch "has_delay" when creating clips
    download: bool = True
    vertical: bool = True
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
            has_delay=_env_bool("HAS_DELAY", d.has_delay),
            download=_env_bool("DOWNLOAD", d.download),
            vertical=_env_bool("VERTICAL", d.vertical),
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
    def log_path(self) -> Path:
        return self.data_dir / "clips.jsonl"

    def validate(self) -> list[str]:
        problems = []
        if not self.client_id:
            problems.append("TWITCH_CLIENT_ID is not set")
        if self.top_streams < 0 or self.top_streams > 100:
            problems.append("TOP_STREAMS must be between 0 and 100")
        if self.top_streams == 0 and not self.channels:
            problems.append("Nothing to watch: set TOP_STREAMS > 0 or CHANNELS")
        if self.detector.short_window >= self.detector.long_window:
            problems.append("DET_SHORT_WINDOW must be smaller than DET_LONG_WINDOW")
        return problems

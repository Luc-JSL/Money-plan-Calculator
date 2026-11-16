"""Thin Twitch Helix client for the handful of endpoints the bot needs."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import http
from .auth import AuthError, TokenManager

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"


class HelixError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass
class Stream:
    user_id: str
    login: str
    display_name: str
    game_id: str
    game_name: str
    title: str
    viewer_count: int
    language: str


@dataclass
class Clip:
    id: str
    url: str
    embed_url: str
    broadcaster_id: str
    broadcaster_name: str
    creator_id: str
    title: str
    thumbnail_url: str
    created_at: str
    duration: float
    game_id: str = ""


class Helix:
    def __init__(self, client_id: str, tokens: TokenManager, sleep=time.sleep):
        self.client_id = client_id
        self.tokens = tokens
        self.sleep = sleep

    # ----- plumbing -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        t = self.tokens.ensure()
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {t.access_token}"}

    def _call(self, method: str, path: str, params=None, retries: int = 3) -> dict:
        last: http.Response | None = None
        for attempt in range(retries + 1):
            r = http.request(method, f"{HELIX}/{path}", params=params, headers=self._headers())
            last = r
            if r.ok:
                return r.body if isinstance(r.body, dict) else {"data": r.body}
            if r.status == 401 and attempt == 0:
                log.warning("Helix 401 on %s; refreshing token", path)
                self.tokens.force_refresh()
                continue
            if r.status == 429:
                reset = float(r.headers.get("ratelimit-reset", 0) or 0)
                wait = max(1.0, min(60.0, reset - time.time())) if reset else 5.0
                log.warning("Helix rate limited; sleeping %.0fs", wait)
                self.sleep(wait)
                continue
            if r.status >= 500 and attempt < retries:
                self.sleep(2.0 * (attempt + 1))
                continue
            break
        assert last is not None
        msg = last.body.get("message", "") if isinstance(last.body, dict) else str(last.body)
        raise HelixError(last.status, msg)

    # ----- endpoints ------------------------------------------------------
    def get_game_id(self, name: str) -> str | None:
        data = self._call("GET", "games", {"name": name}).get("data", [])
        return data[0]["id"] if data else None

    def get_users(self, logins: list[str]) -> dict[str, str]:
        """login -> user_id for up to 100 logins."""
        out: dict[str, str] = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i:i + 100]
            for u in self._call("GET", "users", {"login": chunk}).get("data", []):
                out[u["login"].lower()] = u["id"]
        return out

    def get_streams(self, *, game_id: str | None = None, logins: list[str] | None = None,
                    languages: list[str] | None = None, first: int = 100) -> list[Stream]:
        params: dict = {"type": "live", "first": max(1, min(100, first))}
        if game_id:
            params["game_id"] = game_id
        if logins:
            params["user_login"] = logins[:100]
        if languages:
            params["language"] = languages
        data = self._call("GET", "streams", params).get("data", [])
        return [
            Stream(
                user_id=s["user_id"], login=s["user_login"].lower(), display_name=s.get("user_name", ""),
                game_id=s.get("game_id", ""), game_name=s.get("game_name", ""), title=s.get("title", ""),
                viewer_count=int(s.get("viewer_count", 0)), language=s.get("language", ""),
            )
            for s in data
        ]

    def create_clip(self, broadcaster_id: str, has_delay: bool = False) -> tuple[str, str]:
        """Returns (clip_id, edit_url). Twitch captures ~90s around the request
        time (roughly 25s before and 5s after is the default published window)."""
        body = self._call("POST", "clips", {"broadcaster_id": broadcaster_id,
                                            "has_delay": "true" if has_delay else "false"})
        data = body.get("data") or []
        if not data:
            raise HelixError(200, "create clip returned no data")
        return data[0]["id"], data[0].get("edit_url", "")

    def get_clips(self, ids: list[str]) -> list[Clip]:
        data = self._call("GET", "clips", {"id": ids[:100]}).get("data", [])
        return [
            Clip(
                id=c["id"], url=c.get("url", ""), embed_url=c.get("embed_url", ""),
                broadcaster_id=c.get("broadcaster_id", ""), broadcaster_name=c.get("broadcaster_name", ""),
                creator_id=c.get("creator_id", ""), title=c.get("title", ""),
                thumbnail_url=c.get("thumbnail_url", ""), created_at=c.get("created_at", ""),
                duration=float(c.get("duration", 0) or 0), game_id=c.get("game_id", ""),
            )
            for c in data
        ]

    def wait_for_clip(self, clip_id: str, timeout: float = 30.0, interval: float = 2.0) -> Clip | None:
        """Clip creation is asynchronous; poll until Get Clips returns it."""
        deadline = time.time() + timeout
        while True:
            clips = self.get_clips([clip_id])
            if clips:
                return clips[0]
            if time.time() >= deadline:
                return None
            self.sleep(interval)


__all__ = ["Helix", "HelixError", "AuthError", "Stream", "Clip"]

"""Twitch OAuth: Device Code Grant flow + token storage + refresh.

The Device Code flow needs no redirect URL or web server: the bot prints a
URL and a short code, you approve it in a browser, and the bot receives a
user access token with the `clips:edit` scope (required by Create Clip).

Works with a "Public" Twitch application (no client secret). If your app is
"Confidential", set TWITCH_CLIENT_SECRET and it is included on refresh.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import http

log = logging.getLogger(__name__)

ID_BASE = "https://id.twitch.tv/oauth2"
SCOPES = ["clips:edit"]
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class AuthError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float          # unix time
    scopes: list[str]
    user_id: str = ""
    login: str = ""

    def expires_soon(self, margin: float = 300.0) -> bool:
        return time.time() + margin >= self.expires_at


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Tokens | None:
        if not self.path.is_file():
            return None
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return Tokens(**d)
        except (ValueError, TypeError) as e:
            log.warning("token file %s is unreadable (%s); ignoring", self.path, e)
            return None

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.path)


def start_device_flow(client_id: str, scopes: list[str] | None = None) -> dict:
    """Step 1: ask Twitch for a device code. Returns the raw response dict with
    device_code, user_code, verification_uri, expires_in, interval."""
    r = http.request(
        "POST", f"{ID_BASE}/device",
        form={"client_id": client_id, "scopes": " ".join(scopes or SCOPES)},
    )
    if not r.ok or not isinstance(r.body, dict) or "device_code" not in r.body:
        raise AuthError(f"device flow start failed: HTTP {r.status} {r.body}")
    return r.body


def poll_device_flow(client_id: str, device_code: str, interval: float, expires_in: float,
                     scopes: list[str] | None = None, sleep=time.sleep) -> Tokens:
    """Step 2: poll until the user approves. Raises AuthError on expiry/denial."""
    deadline = time.time() + expires_in
    while time.time() < deadline:
        r = http.request(
            "POST", f"{ID_BASE}/token",
            form={
                "client_id": client_id,
                "scopes": " ".join(scopes or SCOPES),
                "device_code": device_code,
                "grant_type": DEVICE_GRANT,
            },
        )
        if r.ok and isinstance(r.body, dict) and "access_token" in r.body:
            return _tokens_from_response(r.body)
        msg = (r.body.get("message") if isinstance(r.body, dict) else str(r.body)) or ""
        if "authorization_pending" in msg or r.status == 400 and not msg:
            sleep(interval)
            continue
        if "slow_down" in msg:
            interval += 5
            sleep(interval)
            continue
        raise AuthError(f"device flow failed: HTTP {r.status} {msg}")
    raise AuthError("device code expired before it was approved; run `auth` again")


def refresh(client_id: str, refresh_token: str, client_secret: str = "") -> Tokens:
    form = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_secret:
        form["client_secret"] = client_secret
    r = http.request("POST", f"{ID_BASE}/token", form=form)
    if not r.ok or not isinstance(r.body, dict) or "access_token" not in r.body:
        raise AuthError(f"token refresh failed: HTTP {r.status} {r.body}")
    return _tokens_from_response(r.body)


def validate(access_token: str) -> dict | None:
    """Returns {login, user_id, scopes, expires_in, client_id} or None if invalid."""
    r = http.request("GET", f"{ID_BASE}/validate", headers={"Authorization": f"OAuth {access_token}"})
    if r.ok and isinstance(r.body, dict):
        return r.body
    return None


def _tokens_from_response(body: dict) -> Tokens:
    scopes = body.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", ""),
        expires_at=time.time() + float(body.get("expires_in", 3600)),
        scopes=list(scopes),
    )


class TokenManager:
    """Keeps a valid user token on hand, refreshing and persisting as needed."""

    def __init__(self, client_id: str, store: TokenStore, client_secret: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.store = store
        self.tokens = store.load()

    def ensure(self) -> Tokens:
        if self.tokens is None:
            raise AuthError("no saved token; run `python -m clipbot auth` first")
        if self.tokens.expires_soon():
            self.force_refresh()
        return self.tokens

    def force_refresh(self) -> Tokens:
        if self.tokens is None or not self.tokens.refresh_token:
            raise AuthError("no refresh token; run `python -m clipbot auth` again")
        log.info("refreshing Twitch user token")
        new = refresh(self.client_id, self.tokens.refresh_token, self.client_secret)
        new.user_id, new.login = self.tokens.user_id, self.tokens.login
        self.tokens = new
        self.store.save(new)
        return new

    def interactive_login(self, print_fn=print, sleep=time.sleep) -> Tokens:
        info = start_device_flow(self.client_id)
        print_fn("")
        print_fn("Open this URL in a browser and approve the request:")
        print_fn(f"  {info.get('verification_uri')}")
        print_fn(f"Code: {info.get('user_code')}")
        print_fn("")
        tokens = poll_device_flow(
            self.client_id, info["device_code"],
            float(info.get("interval", 5)), float(info.get("expires_in", 1800)), sleep=sleep,
        )
        v = validate(tokens.access_token) or {}
        tokens.user_id = str(v.get("user_id", ""))
        tokens.login = str(v.get("login", ""))
        if "clips:edit" not in tokens.scopes:
            raise AuthError(f"token is missing clips:edit scope (got {tokens.scopes})")
        self.tokens = tokens
        self.store.save(tokens)
        print_fn(f"Logged in as {tokens.login or tokens.user_id}. Token saved to {self.store.path}")
        return tokens

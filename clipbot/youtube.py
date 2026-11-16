"""YouTube Shorts upload through the official YouTube Data API v3.

Uses the OAuth "installed app" loopback flow: `python -m clipbot youtube-auth`
opens a Google consent page, Google redirects to a tiny local server, the bot
stores a refresh token in data/youtube_tokens.json. Uploads use the resumable
protocol, so no Google client library is needed.

Setup (once, about 5 minutes):
  1. console.cloud.google.com -> new project -> APIs & Services -> Enable
     "YouTube Data API v3".
  2. OAuth consent screen -> External -> add yourself as a test user.
  3. Credentials -> Create credentials -> OAuth client ID -> Desktop app.
     Put the client id/secret in YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET.

Quota: a fresh project gets 10,000 units/day and an upload costs ~1,600,
so about 6 uploads per day. Videos under 60s in 9:16 become Shorts.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import secrets
import stat
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path

from . import http as h

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CHUNK = 8 * 1024 * 1024


class YouTubeError(RuntimeError):
    pass


@dataclass
class GoogleTokens:
    access_token: str
    refresh_token: str
    expires_at: float

    def expires_soon(self, margin: float = 120.0) -> bool:
        return time.time() + margin >= self.expires_at


class GoogleTokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> GoogleTokens | None:
        if not self.path.is_file():
            return None
        try:
            return GoogleTokens(**json.loads(self.path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            return None

    def save(self, t: GoogleTokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(t), indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.path)


def _exchange(client_id: str, client_secret: str, form: dict) -> GoogleTokens:
    form = {"client_id": client_id, "client_secret": client_secret, **form}
    r = h.request("POST", TOKEN_URL, form=form)
    if not r.ok or not isinstance(r.body, dict) or "access_token" not in r.body:
        raise YouTubeError(f"token request failed: HTTP {r.status} {r.body}")
    return GoogleTokens(
        access_token=r.body["access_token"],
        refresh_token=r.body.get("refresh_token", form.get("refresh_token", "")),
        expires_at=time.time() + float(r.body.get("expires_in", 3600)),
    )


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    state: str = ""
    error: str | None = None

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] != self.state:
            self.send_response(400); self.end_headers(); self.wfile.write(b"state mismatch"); return
        if "code" in q:
            _CodeCatcher.code = q["code"][0]
            msg = b"clipbot: YouTube connected. You can close this tab."
        else:
            _CodeCatcher.error = q.get("error", ["unknown"])[0]
            msg = b"clipbot: authorization failed."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *a):  # silence
        pass


def interactive_login(client_id: str, client_secret: str, store: GoogleTokenStore, print_fn=print,
                      open_browser=True, timeout: float = 300.0) -> GoogleTokens:
    """Loopback OAuth flow. Must run on a machine with a browser."""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _CodeCatcher)
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}"
    _CodeCatcher.state = secrets.token_urlsafe(16)
    _CodeCatcher.code = _CodeCatcher.error = None
    params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
              "access_type": "offline", "prompt": "consent", "state": _CodeCatcher.state}
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print_fn("\nOpen this URL in a browser and approve YouTube upload access:\n  " + url + "\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + timeout
    try:
        while _CodeCatcher.code is None and _CodeCatcher.error is None and time.time() < deadline:
            time.sleep(0.3)
    finally:
        srv.shutdown()
    if _CodeCatcher.code is None:
        raise YouTubeError(f"authorization not completed ({_CodeCatcher.error or 'timeout'})")
    tokens = _exchange(client_id, client_secret, {"code": _CodeCatcher.code, "grant_type": "authorization_code",
                                                   "redirect_uri": redirect})
    if not tokens.refresh_token:
        raise YouTubeError("Google returned no refresh token; remove the app at myaccount.google.com/permissions and retry")
    store.save(tokens)
    print_fn(f"YouTube connected. Token saved to {store.path}")
    return tokens


class YouTube:
    def __init__(self, client_id: str, client_secret: str, store: GoogleTokenStore):
        self.client_id, self.client_secret, self.store = client_id, client_secret, store
        self.tokens = store.load()

    def _access_token(self) -> str:
        if self.tokens is None:
            raise YouTubeError("YouTube not connected; run `python -m clipbot youtube-auth`")
        if self.tokens.expires_soon():
            self.tokens = _exchange(self.client_id, self.client_secret,
                                    {"grant_type": "refresh_token", "refresh_token": self.tokens.refresh_token})
            self.store.save(self.tokens)
        return self.tokens.access_token

    def upload(self, video: Path, title: str, description: str = "", tags: list[str] | None = None,
               privacy: str = "public", category_id: str = "20") -> str:
        """Resumable upload. Returns the YouTube video URL. category 20 = Gaming."""
        video = Path(video)
        size = video.stat().st_size
        meta = {"snippet": {"title": title[:100], "description": description[:5000], "tags": (tags or [])[:30],
                            "categoryId": category_id},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        headers = {"Authorization": f"Bearer {self._access_token()}", "X-Upload-Content-Type": "video/mp4",
                   "X-Upload-Content-Length": str(size)}
        r = h.request("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                      headers=headers, json_body=meta)
        if not r.ok or "location" not in r.headers:
            msg = r.body.get("error", {}).get("message", r.body) if isinstance(r.body, dict) else r.body
            raise YouTubeError(f"upload session failed: HTTP {r.status} {msg}")
        session = r.headers["location"]
        with video.open("rb") as f:
            sent = 0
            while sent < size:
                chunk = f.read(CHUNK)
                end = sent + len(chunk) - 1
                req = urllib.request.Request(session, data=chunk, method="PUT", headers={
                    "Content-Length": str(len(chunk)), "Content-Range": f"bytes {sent}-{end}/{size}",
                    "Content-Type": "video/mp4"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        status, body = resp.status, resp.read()
                except urllib.error.HTTPError as e:
                    status, body = e.code, e.read()
                if status in (200, 201):
                    vid = json.loads(body.decode("utf-8", errors="replace")).get("id", "")
                    log.info("youtube: uploaded %s -> https://youtube.com/shorts/%s", video.name, vid)
                    return f"https://youtube.com/shorts/{vid}"
                if status != 308:
                    raise YouTubeError(f"chunk upload failed: HTTP {status} {body[:300]!r}")
                sent = end + 1
        raise YouTubeError("upload ended without a video id")

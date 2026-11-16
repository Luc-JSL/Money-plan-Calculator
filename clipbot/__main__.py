"""Command-line interface: python -m clipbot <command>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__, captions, post
from .auth import AuthError, TokenManager, TokenStore, validate
from .config import Config, load_dotenv
from .media import have_ffmpeg, have_ytdlp
from .runner import Runner
from .twitch_api import Helix, HelixError


def setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(cfg.data_dir / "clipbot.log", encoding="utf-8")]
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO), format=fmt, handlers=handlers)


def cmd_auth(cfg: Config) -> int:
    tm = TokenManager(cfg.client_id, TokenStore(cfg.token_path), cfg.client_secret)
    try:
        tm.interactive_login()
    except AuthError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(cfg: Config) -> int:
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'OK ' if good else 'BAD'}] {label}{'' if good else ' - ' + hint}")

    for p in cfg.validate():
        check(p, False, "fix .env")
    check("TWITCH_CLIENT_ID set", bool(cfg.client_id), "create an app at dev.twitch.tv/console/apps")
    check("ffmpeg available", have_ffmpeg(), "install ffmpeg (needed for VERTICAL=1)")
    check("yt-dlp available", have_ytdlp(), "pip install yt-dlp (needed for DOWNLOAD=1)")
    if cfg.captions:
        print(f"[{'OK ' if captions.available() else '-- '}] faster-whisper for captions"
              f"{'' if captions.available() else ' - optional: pip install faster-whisper'}")
    if cfg.auto_post:
        check("tiktok-uploader installed", post.available(), "pip install tiktok-uploader && playwright install chromium")
        check("TIKTOK_COOKIES file exists", bool(cfg.tiktok_cookies) and os.path.isfile(cfg.tiktok_cookies),
              "export cookies.txt from tiktok.com (see README)")
    tokens = TokenStore(cfg.token_path).load()
    check("saved Twitch token", tokens is not None, "run: python -m clipbot auth")
    if tokens:
        v = validate(tokens.access_token)
        check("token valid on Twitch", v is not None, "run: python -m clipbot auth")
        if v:
            check("token has clips:edit", "clips:edit" in (v.get("scopes") or []), "run auth again")
            print(f"      logged in as {v.get('login')} (user_id {v.get('user_id')})")
    if tokens and cfg.client_id:
        try:
            helix = Helix(cfg.client_id, TokenManager(cfg.client_id, TokenStore(cfg.token_path), cfg.client_secret))
            gid = cfg.game_id or helix.get_game_id(cfg.game_name)
            check(f"game {cfg.game_name!r} exists on Twitch", bool(gid),
                  "category not live yet; set CHANNELS to watch specific streamers meanwhile")
        except (HelixError, AuthError) as e:
            check("Helix API reachable", False, str(e))
    print("\nAll good." if ok else "\nFix the BAD lines above, then run `python -m clipbot run`.")
    return 0 if ok else 1


def cmd_streams(cfg: Config) -> int:
    runner = Runner(cfg)
    asyncio.run(runner.resolve_game())
    top = runner.helix.get_streams(game_id=runner.game_id, languages=cfg.languages or None) if runner.game_id else []
    pinned = runner.helix.get_streams(logins=cfg.channels) if cfg.channels else []
    chosen = runner.pick_streams(top, pinned)
    if not chosen:
        print("No live streams match (game not live yet or CHANNELS offline).")
        return 0
    for s in sorted(chosen.values(), key=lambda s: -s.viewer_count):
        print(f"{s.viewer_count:>8,}  {s.login:<25} [{s.language}] {s.title[:70]}")
    return 0


def cmd_clip(cfg: Config, channel: str) -> int:
    runner = Runner(cfg)
    try:
        rec = runner.clip_now(channel.lower().lstrip("#"))
    except (RuntimeError, HelixError, AuthError) as e:
        print(f"Clip failed: {e}", file=sys.stderr)
        return 1
    print(rec.url if rec else "no clip")
    return 0


def cmd_run(cfg: Config) -> int:
    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"config error: {p}", file=sys.stderr)
        return 2
    runner = Runner(cfg)
    try:
        runner.helix.tokens.ensure()
    except AuthError as e:
        print(f"{e}", file=sys.stderr)
        return 1

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, runner.stop)
            except NotImplementedError:  # Windows
                pass
        logging.getLogger("clipbot").info("clipbot %s starting; game=%r top=%d channels=%s",
                                          __version__, cfg.game_name, cfg.top_streams, cfg.channels)
        await runner.run()

    asyncio.run(main())
    return 0


def cmd_post(cfg: Config, video: str, description: str) -> int:
    ok = post.post_tiktok(video, description, cfg.tiktok_cookies)
    print("posted" if ok else "post failed (see log)")
    return 0 if ok else 1


def cmd_history(cfg: Config, limit: int) -> int:
    from .storage import ClipLog
    recs = ClipLog(cfg.log_path).read()
    for r in recs[-limit:]:
        flag = " [posted]" if r.posted else ""
        print(f"{r.created_at or '?':<22} {r.channel:<20} {r.reason:<13} score={r.score:<6} {r.url}{flag}")
    print(f"{len(recs)} clips total")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipbot", description="Automatic Twitch clipper")
    ap.add_argument("--env", default=".env", help="path to .env file (default: .env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="log in to Twitch (device code flow)")
    sub.add_parser("doctor", help="check configuration, tools and token")
    sub.add_parser("streams", help="show which live streams would be monitored")
    sub.add_parser("run", help="run the bot")
    p = sub.add_parser("clip", help="create a clip of a channel right now")
    p.add_argument("channel")
    p = sub.add_parser("history", help="list clips made so far")
    p.add_argument("-n", type=int, default=50)
    p = sub.add_parser("post", help="post a vertical clip to TikTok (needs tiktok-uploader + TIKTOK_COOKIES)")
    p.add_argument("video")
    p.add_argument("-d", "--description", default="")
    args = ap.parse_args(argv)

    load_dotenv(args.env)
    cfg = Config.from_env()
    setup_logging(cfg)
    if args.cmd == "auth":
        return cmd_auth(cfg)
    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "streams":
        return cmd_streams(cfg)
    if args.cmd == "clip":
        return cmd_clip(cfg, args.channel)
    if args.cmd == "history":
        return cmd_history(cfg, args.n)
    if args.cmd == "post":
        return cmd_post(cfg, args.video, args.description)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())

"""Command-line interface: python -m clipbot <command>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__, captions, layout, post
from .post import Poster
from .storage import ClipLog
from .auth import AuthError, TokenManager, TokenStore, validate
from .config import Config, load_dotenv
from .media import have_ffmpeg, have_ytdlp
from .recorder import have_streamlink
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
    print(f"      mode: {cfg.clip_mode} ({'streamlink buffer, no token' if cfg.local_mode else 'Twitch Create Clip API'})")
    check("ffmpeg available", have_ffmpeg(), "install ffmpeg")
    if cfg.local_mode:
        check("streamlink available", have_streamlink(), "pip install streamlink")
        check("CHANNELS or Twitch app for discovery", bool(cfg.channels) or bool(cfg.client_id),
              "set CHANNELS=login1,login2 in .env")
    else:
        check("TWITCH_CLIENT_ID set", bool(cfg.client_id), "create an app at dev.twitch.tv/console/apps")
        check("yt-dlp available", have_ytdlp(), "pip install yt-dlp (needed for DOWNLOAD=1)")
    try:
        import cv2  # type: ignore  # noqa: F401
        has_cv = True
    except ImportError:
        has_cv = False
    model_ok = layout.default_model_path(cfg.data_dir).is_file()
    if cfg.layout != "blurred" and not cfg.facecam_box:
        print(f"[{'OK ' if has_cv and model_ok else '-- '}] facecam detection for the {cfg.layout} layout"
              + ("" if has_cv and model_ok else
                 " - optional: pip install opencv-python-headless && python -m clipbot setup-face"))
    if cfg.captions:
        print(f"[{'OK ' if captions.available() else '-- '}] faster-whisper for captions"
              f"{'' if captions.available() else ' - optional: pip install faster-whisper'}")
    if "tiktok" in cfg.auto_post:
        check("tiktok-uploader installed", post.tiktok_available(), "pip install tiktok-uploader && playwright install chromium")
        check("TIKTOK_COOKIES file exists", bool(cfg.tiktok_cookies) and os.path.isfile(cfg.tiktok_cookies),
              "export cookies.txt from tiktok.com (see README)")
    if "youtube" in cfg.auto_post:
        check("YouTube connected", (cfg.data_dir / "youtube_tokens.json").is_file(), "run: python -m clipbot youtube-auth")
    if cfg.auto_post:
        print(f"      auto-post: {', '.join(cfg.auto_post)} (max {cfg.max_posts_per_day}/day each)")
    tokens = TokenStore(cfg.token_path).load()
    if cfg.local_mode and tokens is None:
        print("[-- ] no Twitch token - fine in local mode; add one (`auth`) to auto-discover top streams")
    else:
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
    if runner.helix is None:
        print("No Twitch app/token: the bot will watch CHANNELS = " + ", ".join(cfg.channels))
        return 0
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
    if runner.helix is not None:
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
        logging.getLogger("clipbot").info("clipbot %s starting; mode=%s game=%r top=%d channels=%s",
                                          __version__, cfg.clip_mode, cfg.game_name, cfg.top_streams, cfg.channels)
        await runner.run()

    asyncio.run(main())
    return 0


def cmd_setup_face(cfg: Config) -> int:
    dest = layout.default_model_path(cfg.data_dir)
    if dest.is_file():
        print(f"face model already at {dest}")
        return 0
    print(f"downloading YuNet face model to {dest} ...")
    try:
        layout.download_model(dest)
    except Exception as e:  # noqa: BLE001
        print(f"download failed: {e}\nGet it manually from {layout.YUNET_URL} and save it as {dest}", file=sys.stderr)
        return 1
    print("done")
    return 0


def cmd_render(cfg: Config, video: str, out: str | None) -> int:
    """Re-render one clip with the current LAYOUT / FACECAM_* / CAPTIONS settings."""
    from .media import render_vertical
    src = Path(video)
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    box = layout.parse_box(cfg.facecam_box) if cfg.facecam_box else \
        layout.find_facecam(src, layout.yunet_detector(layout.default_model_path(cfg.data_dir)))
    graph, used = layout.choose_graph(cfg.layout, box, cfg.facecam_share)
    ass = captions.make_captions(src, src.with_suffix(".ass"), model_size=cfg.whisper_model) if cfg.captions else None
    dest = Path(out) if out else src.with_name(src.stem.replace("-vertical", "") + "-vertical.mp4")
    result = render_vertical(src, dest, ass_path=ass, graph=graph)
    print(f"{used} layout, facecam={box}, captions={'yes' if ass else 'no'} -> {result or 'FAILED'}")
    return 0 if result else 1


def cmd_youtube_auth(cfg: Config) -> int:
    from .youtube import GoogleTokenStore, YouTubeError, interactive_login
    if not (cfg.youtube_client_id and cfg.youtube_client_secret):
        print("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first (see README section 5).", file=sys.stderr)
        return 2
    try:
        interactive_login(cfg.youtube_client_id, cfg.youtube_client_secret,
                          GoogleTokenStore(cfg.data_dir / "youtube_tokens.json"))
    except YouTubeError as e:
        print(f"YouTube login failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_post(cfg: Config, video: str | None, description: str, to: list[str], pending: bool, limit: int) -> int:
    platforms = to or cfg.auto_post
    if not platforms:
        print("Say where to post: --to youtube, --to tiktok (or set AUTO_POST in .env)", file=sys.stderr)
        return 2
    cfg.auto_post = platforms
    poster = Poster(cfg)
    if pending:
        results = poster.post_pending(ClipLog(cfg.log_path).read(), limit=limit)
        for r in results:
            print(f"{r.platform:<8} {'ok ' if r.ok else 'FAIL'} {r.url}")
        if not results:
            print("nothing to post (queue empty or daily cap reached)")
        return 0 if all(r.ok for r in results) else 1
    if not video:
        print("give a video file or --pending", file=sys.stderr)
        return 2
    ok = True
    for p in platforms:
        r = poster.post(Path(video), p, description or cfg.post_template.format(
            display_name="", login="", game=cfg.game_name, title="", url=""), force=True)
        print(f"{p:<8} {'ok ' if r.ok else ('skip (' + r.skipped + ')' if r.skipped else 'FAIL')} {r.url}")
        ok &= r.ok or r.skipped == "already"
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
    sub.add_parser("youtube-auth", help="connect a YouTube channel for Shorts uploads")
    sub.add_parser("setup-face", help="download the face model used to find the streamer's webcam")
    p = sub.add_parser("render", help="re-render a clip as vertical with the current layout/caption settings")
    p.add_argument("video")
    p.add_argument("-o", "--out")
    p = sub.add_parser("post", help="post a vertical clip (or the pending queue) to YouTube Shorts / TikTok")
    p.add_argument("video", nargs="?")
    p.add_argument("-d", "--description", default="")
    p.add_argument("--to", action="append", choices=["youtube", "tiktok"], default=[])
    p.add_argument("--pending", action="store_true", help="post the best unposted clips up to the daily cap")
    p.add_argument("-n", "--limit", type=int, default=0)
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
        return cmd_post(cfg, args.video, args.description, args.to, args.pending, args.limit)
    if args.cmd == "youtube-auth":
        return cmd_youtube_auth(cfg)
    if args.cmd == "setup-face":
        return cmd_setup_face(cfg)
    if args.cmd == "render":
        return cmd_render(cfg, args.video, args.out)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())

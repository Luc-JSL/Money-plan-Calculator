#!/usr/bin/env python3
"""One-command launcher: watch Twitch channels and clip the last 30 seconds
whenever their chat explodes. No Twitch login needed.

    python clip30.py xqc kaicenat          # watch these channels
    python clip30.py xqc --seconds 45      # longer look-back

Needs: pip install -r requirements.txt   (streamlink)   and ffmpeg on PATH.
Clips land in data/clips/<channel>/. Everything else (captions, vertical
render, Discord, tuning) is configured through .env; see README-clipbot.md.
"""
import argparse
import os
import sys

from clipbot.__main__ import main as clipbot_main


def main() -> int:
    ap = argparse.ArgumentParser(description="Clip the last N seconds of a Twitch stream when chat explodes.")
    ap.add_argument("channels", nargs="*", help="Twitch logins to watch (or set CHANNELS in .env)")
    ap.add_argument("--seconds", type=float, default=None, help="seconds before the explosion to keep (default 30)")
    ap.add_argument("--quality", default=None, help="streamlink quality, default 720p60,720p,480p,best")
    ap.add_argument("--no-vertical", action="store_true", help="skip the 9:16 render")
    args = ap.parse_args()

    os.environ["CLIP_MODE"] = "local"
    if args.channels:
        os.environ["CHANNELS"] = ",".join(c.lower().lstrip("#") for c in args.channels)
    if args.seconds:
        os.environ["CLIP_PRE_SECONDS"] = str(args.seconds)
    if args.quality:
        os.environ["QUALITY"] = args.quality
    if args.no_vertical:
        os.environ["VERTICAL"] = "false"
    return clipbot_main(["run"])


if __name__ == "__main__":
    sys.exit(main())

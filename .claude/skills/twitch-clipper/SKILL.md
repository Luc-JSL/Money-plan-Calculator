---
name: twitch-clipper
description: Operate, tune, debug and extend the Twitch auto-clipper in this repo (clipbot package). Use when the user mentions clipping, Twitch clips, viral moments, GTA 6 streams, the clip bot, chat spike detection, running or deploying the bot, or reviewing clips it made.
---

# Twitch clipper (clipbot)

The bot lives in `clipbot/` and is documented in `README-clipbot.md`. Read that file
first if anything below is unclear.

## Architecture (one line each)
- `config.py`: all settings come from env / `.env`; `Config.validate()` lists problems.
- `auth.py`: Twitch device-code login, token store (`data/tokens.json`), auto refresh.
- `twitch_api.py`: Helix client: games, streams, users, create clip, get clips, wait_for_clip.
- `irc.py`: anonymous TMI chat reader; `ChatPool.set_channels()` joins/parts; `parse_line()`.
- `detector.py`: `SpikeDetector.observe()` returns a `Trigger`; pure logic with injectable clock.
- `media.py`: `download_clip()` via yt-dlp, `render_vertical(ass_path=...)` via ffmpeg.
- `layout.py`: finds the streamer's webcam (YuNet + geometry) and builds the split / stack /
  blurred ffmpeg graphs; `choose_graph()` decides. `clipbot render <clip>` re-renders one clip.
- `captions.py`: faster-whisper words -> `group_words()` -> `write_ass()`; injectable transcriber.
- `retention.py`: `enforce(root, max_gb, max_clips)` prunes oldest clip groups.
- `post.py`: `Poster` posts vertical clips to platforms in `AUTO_POST` with a per-day cap and a
  `PostState` file; `post_pending()` drains the queue. TikTok via `tiktok-uploader` (risky).
- `youtube.py`: official YouTube Data API: loopback OAuth (`youtube-auth`) + resumable upload.
- `recorder.py`: local mode. `Recorder` supervises `streamlink --stdout | ffmpeg -f segment` into a
  rolling buffer of 2s TS files; `cut()` joins and re-encodes the last N seconds into an MP4 (GPU encoder if present, libx264 fallback).
- `runner.py`: `Runner` ties discovery, chat and clipping together. Local mode:
  `make_clip_local()` -> `_finish()`; twitch mode: `make_clip()` -> `_finish()`.
- `__main__.py`: CLI `auth | doctor | streams | clip <login> | run | history`.

## Modes
`CLIP_MODE=local` (default) needs no Twitch login: streamlink + ffmpeg + anonymous chat.
Without a Twitch app it watches exactly `CHANNELS`. `CLIP_MODE=twitch` uses the Create
Clip API and needs `auth`. A Twitch app + token enables `TOP_STREAMS` discovery in both.

## Operating checklist
1. `python -m clipbot doctor` must print `All good.` before `run`.
2. Local mode: make sure `CHANNELS` is set and streamlink + ffmpeg are installed.
   Twitch mode / discovery: `python -m clipbot auth` once (needs a browser).
3. `python -m clipbot streams` shows what would be watched; empty output means the game
   category has no live streams and `CHANNELS` is empty or offline.
4. `python -m clipbot run` is the long-running process. In Docker: `docker compose up -d`.
5. Review output: `python -m clipbot history`, `data/clips.jsonl`, `data/clips/<channel>/`.

## Tuning
Detector knobs are `DET_*` variables in `.env` (documented in `.env.example`).
When a user says "too many clips" raise `DET_SPIKE_MULTIPLIER` / `DET_MIN_HYPE_FRACTION` /
`DET_SUSTAIN`; "missing moments" lower them; "clips start too late" set `HAS_DELAY=true`;
"it clipped a raid" check `DET_RAID_SUPPRESS` and the raid USERNOTICE parsing in `irc.py`.
Always run `python -m unittest discover -s tests` after changing `detector.py` and add a
test case that reproduces the chat pattern in question (see `tests/test_detector.py`,
helper `chatter()`).

## Editing
For hand edits (hook text, tighter cuts) point the user to Clips Kitty (ColinGPT9/clips-studio,
AGPL: use it as a tool, never copy its code here). Its Gaming/Reaction mode matches our split
layout. "Facecam band is wrong" -> `FACECAM_BOX=x,y,w,h` then `clipbot render`.

## Rules
- Video comes from streamlink (public HLS) or the Helix Create Clip API; chat from anonymous
  IRC. Never add GQL scraping with hard-coded web client ids or browser-cookie tricks.
- The recorder test spins up a local HLS stream; run `tests.test_recorder` after touching
  `recorder.py`.
- Network calls go through `clipbot/http.py:request` so tests can stub them; keep it that way.
- Never turn on `AUTO_POST` yourself; it is the user's call. YouTube is safe (official API);
  TikTok carries a ban risk. Keep `MAX_POSTS_PER_DAY` small.
- Related repos and what was borrowed are listed in README-clipbot.md section 6.
- Never commit `.env`, `data/` or clip files.
- Keep the test suite green; CI runs it on every push.

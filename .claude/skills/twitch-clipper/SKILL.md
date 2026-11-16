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
- `media.py`: `download_clip()` via yt-dlp, `render_vertical()` via ffmpeg.
- `runner.py`: `Runner` ties discovery, chat and clipping together; `make_clip()` is the pipeline.
- `__main__.py`: CLI `auth | doctor | streams | clip <login> | run | history`.

## Operating checklist
1. `python -m clipbot doctor` must print `All good.` before `run`.
2. If the token is missing or invalid: `python -m clipbot auth` (needs a browser once).
3. `python -m clipbot streams` shows what would be watched; empty output means the game
   category has no live streams and `CHANNELS` is empty or offline.
4. `python -m clipbot run` is the long-running process. In Docker: `docker compose up -d`.
5. Review output: `python -m clipbot history`, `data/clips.jsonl`, `data/clips/<channel>/`.

## Tuning
Detector knobs are `DET_*` variables in `.env` (documented in `.env.example`).
When a user says "too many clips" raise `DET_SPIKE_MULTIPLIER` / `DET_MIN_HYPE_FRACTION`;
"missing moments" lower them; "clips start too late" set `HAS_DELAY=true`.
Always run `python -m unittest discover -s tests` after changing `detector.py` and add a
test case that reproduces the chat pattern in question (see `tests/test_detector.py`,
helper `chatter()`).

## Rules
- Only official Twitch endpoints (Helix + anonymous IRC). Never add stream recording,
  GQL scraping or browser-token tricks.
- Network calls go through `clipbot/http.py:request` so tests can stub them; keep it that way.
- Do not auto-post to TikTok/YouTube without the user asking; explain the API/ToS limits.
- Never commit `.env`, `data/` or clip files.
- Keep the test suite green; CI runs it on every push.

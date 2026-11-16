# clipbot: automatic Twitch clipper

Reads Twitch chat anonymously (no token), detects the moments when chat explodes, and
clips the **30 seconds** that caused it. Each clip is rendered as a 9:16 vertical video
with burned-in captions for TikTok / Shorts / Reels, logged, and optionally posted to a
Discord channel.

Two ways to get the video, chosen with `CLIP_MODE`:

| | `local` (default) | `twitch` |
|---|---|---|
| Needs a Twitch login | **No** | Yes (one-time device-code login) |
| How | [streamlink](https://streamlink.github.io) keeps a rolling buffer of the stream on disk; ffmpeg cuts the last 30s | Official Create Clip API, then yt-dlp download |
| Clip length | Exactly `CLIP_PRE_SECONDS` (+ `CLIP_POST_SECONDS`) | Twitch's default (~30s) |
| Which streams | `CHANNELS` you list (top-stream discovery needs a Twitch app) | `CHANNELS` + top N live streams of the game |
| Cost | About 1 MB/s download per watched channel at 1080p60 | Almost nothing |

```
channels  ->  streamlink -> ffmpeg rolling buffer (local mode)
          ->  read chat (anonymous IRC)  ->  spike detector  ->  cut last 30 s
          ->  captions (whisper)  ->  vertical render (ffmpeg)
          ->  data/clips.jsonl + Discord webhook  (-> optional TikTok post)
```

## 1. Setup (local mode, no login, about 2 minutes)

Requires Python 3.11+ and ffmpeg on PATH (`sudo apt install ffmpeg`, `brew install ffmpeg`,
or `winget install Gyan.FFmpeg`).

```bash
pip install -r requirements.txt          # streamlink + yt-dlp
pip install faster-whisper               # optional: captions
cp .env.example .env
```

Edit `.env`: put the streamers to watch in `CHANNELS=xqc,kaicenat,...`. That is all.

```bash
python -m clipbot doctor                 # should end with "All good."
python -m clipbot run
```

Or skip `.env` entirely and pass the channels on the command line:

```bash
python clip30.py xqc kaicenat            # clip the last 30 s whenever their chat explodes
python clip30.py xqc --seconds 45        # longer look-back
```

### Optional: Twitch app (top-stream discovery and/or twitch mode)

1. Create an application at https://dev.twitch.tv/console/apps ("Register Your
   Application"): any name, OAuth Redirect URL `http://localhost`, category Application
   Integration, Client Type **Public**. Copy the Client ID into `TWITCH_CLIENT_ID`.
2. `python -m clipbot auth` prints a URL and a code; approve it in a browser. The token is
   saved to `data/tokens.json` and refreshed automatically.
3. Now `TOP_STREAMS=15` makes the bot follow the 15 busiest live GTA VI streams
   automatically, in either clip mode. `CLIP_MODE=twitch` additionally creates real Twitch
   clips (on the account you logged in with) instead of cutting locally.

## 2. Run it

```bash
python -m clipbot run
```

Clips are taken at the stream's source quality (`QUALITY=best`, normally 1080p60), which
matters because the split layout zooms the game about 2x. Watching many channels on a slow
connection? `QUALITY=720p60,720p,best` halves the bandwidth.

In local mode the log shows `recording started` per channel once streamlink finds the
stream; offline channels are retried every 20 seconds until they go live. A trigger looks
like `TRIGGER xqc: spike score=9.4 rate=31.0/s baseline=6.2/s x5.0 users=120 hype=71%`
followed by `CLIP xqc -> data/clips/xqc/20261119-213055-local.mp4`.

Or with Docker (ffmpeg and yt-dlp included):

```bash
docker compose run --rm clipbot auth    # first time only
docker compose up -d                    # runs forever, restarts on crash
docker compose logs -f
```

On a Linux VPS without Docker, `clipbot.service` is a ready systemd unit.

### Other commands

| Command | What it does |
|---|---|
| `python -m clipbot streams` | Show which live streams would be monitored right now |
| `python -m clipbot clip <login>` | Create a Twitch clip of that channel immediately (needs the Twitch app) |
| `python -m clipbot history` | List the clips made so far |
| `python -m clipbot youtube-auth` | Connect a YouTube channel (one-time) |
| `python -m clipbot setup-face` | Download the face model for webcam detection (one-time) |
| `python -m clipbot render <clip.mp4>` | Re-render a clip with the current layout and caption settings |
| `python -m clipbot post --pending` | Post the best unposted clips, up to today's cap |
| `python -m clipbot doctor` | Verify config, token, ffmpeg and yt-dlp |

Logs go to stdout and `data/clipbot.log`. Clips land in `data/clips/<channel>/` as
`<timestamp>-<clipid>.mp4` and `...-vertical.mp4`. Every clip is appended to
`data/clips.jsonl` with the chat statistics that triggered it.

## 3. How detection works

For every watched channel the bot keeps the last 120 seconds of chat and compares the
**last 10 seconds** against the rest:

- the burst rate must be at least `DET_SPIKE_MULTIPLIER` (3x) the baseline rate,
- at least `DET_MIN_UNIQUE_USERS` (5) different people must be talking,
- at least `DET_MIN_HYPE_FRACTION` (35%) of the burst must look like hype: emotes,
  LUL / KEKW / POG style words, "no way", "clip it", all caps, "?????",
- one user can never make up more than half of the burst (spam protection),
- the spike must hold for `DET_SUSTAIN` (3s): a single emote wave is not a moment,
- an incoming raid silences the channel for `DET_RAID_SUPPRESS` (120s), because
  raid spam looks exactly like hype,
- a channel is observed for `DET_WARMUP` (60s) before it may trigger, and waits
  `DET_COOLDOWN` (120s) between clips.

Independently, `DET_CLIP_REQUEST_USERS` (3) different people typing "clip" within the
window triggers a clip immediately.

Chat reacts a few seconds *after* the moment, and the detector needs about three seconds
to confirm a spike, so a 30-second look-back (`CLIP_PRE_SECONDS`) puts the moment roughly
20-25 seconds into the clip with its run-up included. Raise `CLIP_POST_SECONDS` to keep
the reaction too. In twitch mode, Create Clip captures ~25s before the request on its own;
set `HAS_DELAY=true` if those clips consistently come out too early.

Tuning tips:
- Too many boring clips: raise `DET_SPIKE_MULTIPLIER` to 4-5 or `DET_MIN_HYPE_FRACTION` to 0.5.
- Missing moments on huge chats (xQc-sized): lower `DET_SPIKE_MULTIPLIER` to 2.
- Small chats: lower `DET_MIN_UNIQUE_USERS` to 3 and `DET_MIN_SHORT_RATE` to 0.5.

## 4. Making money from the clips (honest version)

The bot gives you a stream of candidate clips, logged and ranked by `score`. Money comes
from what you do with them:

- **Streamer clipping programs.** Many large streamers pay clippers per 1,000 views on
  TikTok / Shorts / Reels, mostly through Whop "Content Rewards" campaigns. Typical
  public rates are roughly $1-5 per 1,000 verified views with per-clip caps. Join a
  streamer's program *before* posting their content; it gets you paid and avoids
  takedowns. Avoid any program that charges an entry fee.
- **Platform payouts.** TikTok Creator Rewards, YouTube Shorts revenue sharing and
  Instagram bonuses all have follower / watch-time thresholds you must reach first.
- **GTA 6 timing.** The game launches **November 19, 2026** (PS5 / Xbox Series). The
  Twitch category will not exist until streams start, so until then set `CHANNELS` to the
  streamers you expect to play it; the bot switches to the category automatically once
  `GAME_NAME` resolves. The first week of launch streams is where clipping demand peaks.
- **Rights.** Clips made with Twitch's clip feature are allowed on Twitch. Re-uploading
  them elsewhere is at the streamer's discretion. Streamers who run clipping programs
  want it; others may issue takedowns. Credit the streamer in every post.

There is no guarantee of income. What the bot removes is the manual work of watching
dozens of streams at once.

## 5. Captions, disk budget and posting

**Layout: facecam on top, game below.** Gaming clips on TikTok and Shorts use a split
layout, not a letterboxed 16:9 frame. With `LAYOUT=auto` (default) the bot looks for the
streamer's webcam in the clip (a small face pinned to a corner that does not move) and
renders the webcam as a band on top with the game zoomed to fill the rest. `LAYOUT=stack`
keeps the whole game frame under the webcam instead; `LAYOUT=blurred` is the plain
letterbox. Detection needs two optional pieces:

```bash
pip install opencv-python-headless
python -m clipbot setup-face        # downloads OpenCV's 230 KB YuNet face model once
```

Without them, or when no webcam is found (VTubers, no cam, cam hidden by the game), the
clip falls back to the blurred layout. If detection picks the wrong spot for a streamer,
set `FACECAM_BOX=x,y,w,h` (the webcam rectangle in 1920x1080 source pixels) and the bot
uses that. `python -m clipbot render data/clips/<channel>/<file>.mp4` re-renders one clip
with the current settings so you can try layouts without waiting for a new trigger.

**Captions.** With `pip install faster-whisper` and `CAPTIONS=true` (default), each clip is
transcribed on the CPU (about 10-20s for a 30s clip with the `small` model) and the words
are burned into the vertical video as big centred captions, four words at a time. Without
faster-whisper the vertical render simply has no captions.

**Disk budget.** `MAX_CLIPS_GB` / `MAX_CLIPS_KEEP` prune the oldest clips (raw, vertical
and captions together) after every new clip, so the bot can run for weeks unattended.

**Posting.** Off by default. The Discord webhook pings you, you open `...-vertical.mp4`,
add a hook line and post. The `score` in `data/clips.jsonl` tells you which ones first.

**Auto-posting** (`AUTO_POST=youtube,tiktok`). Every vertical clip is posted as it is made,
up to `MAX_POSTS_PER_DAY` per platform (default 6). Clips beyond the cap wait in a queue;
`python -m clipbot post --pending` posts the highest-scoring unposted ones, so you can run
it the next morning or from a daily cron. Nothing is ever posted twice
(`data/post_state.json` remembers).

*YouTube Shorts* uses the official YouTube Data API, so it is safe for your channel:

1. console.cloud.google.com: create a project, enable **YouTube Data API v3**.
2. OAuth consent screen: External, add your own Google account as a test user.
3. Credentials: create an **OAuth client ID** of type **Desktop app**. Copy the client id
   and secret into `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`.
4. `python -m clipbot youtube-auth` opens Google's consent page; approve once.

A new project's quota allows about 6 uploads a day, which is why the cap defaults to 6.
Clips under 60 seconds in 9:16 are shown as Shorts automatically; the title comes from
`YOUTUBE_TITLE_TEMPLATE`.

*TikTok* has no public posting API for normal accounts. The bot can use the MIT-licensed
[tiktok-uploader](https://github.com/wkaisertexas/tiktok-uploader) package, which drives a
headless browser with cookies exported from tiktok.com:

```bash
pip install tiktok-uploader && playwright install chromium
# export cookies.txt from tiktok.com with a "Get cookies.txt" browser extension, then:
TIKTOK_COOKIES=/path/cookies.txt
```

Know the risks: this is against TikTok's terms, accounts get rate-limited or banned, and
TikTok changes its upload page often so the package can break. Use a dedicated clips
account, keep `MAX_POSTS_PER_DAY` low, and check the first few posts by hand.

Manual posting of one file: `python -m clipbot post data/clips/xqc/<file>-vertical.mp4 --to youtube -d "caption"`.

## 6. Editing the clips by hand: Clips Kitty

The bot's output is a scout's output: right moment, right layout, captions. The clips
that go viral also get a hook line in the first second, tighter cuts and a title. For that
the best open-source tool we found is **[Clips Kitty](https://github.com/ColinGPT9/clips-studio)**
(ColinGPT9/clips-studio, AGPL, free, Windows app on the Microsoft Store, also Docker):

- built for Twitch and Kick streams: a **Gaming / Reaction** mode with eleven webcam + game
  layouts (split, basecam, small facecam, circle cam, duo...), the streamer kept clear of
  TikTok's buttons;
- word-synced captions in editable styles, hook text, trims, mutes, speed, music, a
  non-destructive editor and an "edit by chat" box (*make it 5 seconds longer*);
- takes local files, Twitch VODs or Kick VODs, and publishes to YouTube, TikTok and
  Instagram on a schedule;
- runs on your own PC with a local model; no per-clip fees.

Workflow: let clipbot collect clips in `data/clips/`, open the best ones (highest `score`
in `clips.jsonl`) in Clips Kitty, add the hook and post. Install:
`winget install ColinGPT9.ClipsKitty` or the Microsoft Store link in its README.

Also worth knowing: [NakhulGithesh/gta6-clipper](https://github.com/NakhulGithesh/gta6-clipper)
(MIT, macOS) turns whole GTA 6 reaction VODs into facecam-on-top clips after the stream;
its webcam-finding geometry is what `clipbot/layout.py` follows.

## 7. Related open-source projects

Reviewed while building this (none are required):

| Project | What it is | What we took |
|---|---|---|
| [Higuy3000/twitch-clipbot](https://github.com/Higuy3000/twitch-clipbot) | Chat-spike clipper that records the stream with streamlink | The "sustain" rule, whisper captions and clip retention ideas (re-implemented; repo has no license) |
| [bihanikeshav/TwitchSnipBot](https://github.com/bihanikeshav/TwitchSnipBot) | Chat-feature + LSTM highlight scorer, web app | Feature ideas (unique-user ratio, repetition) for a future ML scorer |
| [d0xa/ChachoBot](https://github.com/d0xa/ChachoBot) | Chat hype + audio-volume spikes, posts to Discord | Audio spikes need stream recording; not adopted |
| [wkaisertexas/tiktok-uploader](https://github.com/wkaisertexas/tiktok-uploader) | Playwright TikTok uploader (MIT, 780+ stars) | Optional `post` command / `AUTO_POST` |
| [lay295/TwitchDownloader](https://github.com/lay295/TwitchDownloader) | Clip/VOD downloader with chat render (4k stars) | Alternative to yt-dlp if Twitch breaks it; can render chat into the video |
| [kleZ799/clipmint](https://github.com/kleZ799/clipmint) | Local AI clipper for VODs with YouTube upload | Post-stream VOD clipping and YouTube posting, if wanted later |
| [VadlapatiKarthik/autoclipper](https://github.com/VadlapatiKarthik/autoclipper) | Cloud pipeline: Twitch/Kick/YouTube -> subtitles -> YouTube upload | Heavy (Celery, Redis, GPT); reference only |
| [ZiedYT/automatic-twitch-clipper](https://github.com/ZiedYT/automatic-twitch-clipper) | Oldest chat-based clipper | Uses scraped browser tokens; avoided |

## 8. Tests

```bash
python -m unittest discover -s tests -v
```

The suite needs no internet. It covers the detector (bursts, spam, sustain, raids, cooldown,
big chats, reconnect gaps), IRC parsing plus a fake Twitch chat server, the Helix client
(token refresh, 429 / 503 handling, clip polling), the device-code login, the orchestrator
(stream selection, hourly cap, blocked channels, chat-to-clip path), caption grouping and
a real ffmpeg vertical render with burned-in subtitles, retention pruning, posting, and an
end-to-end local-mode run: a live HLS stream generated by ffmpeg and served over HTTP is
pulled by streamlink into the rolling buffer and cut into a clip.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no saved token` | Only needed for `CLIP_MODE=twitch` or `TOP_STREAMS`; run `python -m clipbot auth` |
| `has no buffered video` | streamlink has not found the stream yet (offline, or the first seconds after it went live) |
| `error: No playable streams found` in the log | Channel offline, wrong login, or streamlink needs an update: `pip install -U streamlink` |
| Red `[h264 @ ...] Invalid NAL unit size` lines | Harmless decoder chatter from a damaged file; update to the current version, which re-encodes clips at cut time and silences it |
| Cutting a clip takes long | Clips are encoded on the GPU when ffmpeg has NVENC / QSV / AMF / VideoToolbox, otherwise on the CPU (about 10-20 s per 30 s clip at 1080p60) |
| Buffer eats disk or bandwidth | Lower `BUFFER_SECONDS` (180s is about 180 MB per channel at 1080p60) or set `QUALITY=720p60,720p,best` |
| `game 'Grand Theft Auto VI' not found` | The category is not live yet; use `CHANNELS` meanwhile |
| `cannot clip <channel> (HTTP 403)` | That streamer restricted clipping; the bot skips them for an hour |
| Clips not downloading | `pip install -U yt-dlp` (Twitch changes its CDN often) |
| No vertical files | Install ffmpeg or set `VERTICAL=false` |
| No captions on clips | `pip install faster-whisper`; first run downloads the model |
| Clips are letterboxed, no facecam band | `pip install opencv-python-headless && python -m clipbot setup-face`, or set `FACECAM_BOX` |
| Facecam band shows the wrong area | Set `FACECAM_BOX=x,y,w,h` for that streamer and re-run `clipbot render` |
| Clips fire on raids / emote waves | Raise `DET_SUSTAIN` to 5; raids are already suppressed |
| Too many / too few clips | See tuning tips above; changes in `.env` apply on restart |

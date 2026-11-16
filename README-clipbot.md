# clipbot: automatic Twitch clipper

Watches the busiest live streams of a game (default: **Grand Theft Auto VI**), reads their
chat, detects the moments when chat explodes, and creates a Twitch clip of that moment
through the official Twitch API. Each clip is downloaded, rendered as a 9:16 vertical
video for TikTok / Shorts / Reels, logged, and optionally posted to a Discord channel.

```
discover top streams  ->  read chat (anonymous IRC)  ->  spike detector
      -> Twitch "Create Clip"  ->  download (yt-dlp)  ->  vertical render (ffmpeg)
      -> data/clips.jsonl + Discord webhook
```

Everything uses official, ToS-compliant Twitch endpoints. No stream recording, no
scraped browser tokens.

## 1. One-time setup (about 5 minutes)

1. **Create a Twitch application** at https://dev.twitch.tv/console/apps
   ("Register Your Application"):
   - Name: anything, e.g. `my-clipbot`
   - OAuth Redirect URL: `http://localhost` (required by the form, not used)
   - Category: Application Integration
   - Client Type: **Public**
   - Copy the **Client ID**.
2. **Install** (Python 3.11+ and ffmpeg):
   ```bash
   pip install -r requirements.txt
   cp .env.example .env        # then paste your Client ID into TWITCH_CLIENT_ID
   ```
3. **Log in** (device code flow, no web server needed):
   ```bash
   python -m clipbot auth
   ```
   It prints a URL and a code. Open the URL, enter the code, approve. The token is saved
   to `data/tokens.json` and refreshed automatically from then on. The Twitch account you
   log in with is the account the clips are created under.
4. **Check everything:**
   ```bash
   python -m clipbot doctor
   ```

## 2. Run it

```bash
python -m clipbot run
```

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
| `python -m clipbot clip <login>` | Create a clip of that channel immediately |
| `python -m clipbot history` | List the clips made so far |
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

Twitch's Create Clip captures the stream around the moment of the request (about 25
seconds before it and a few after), so triggering as soon as chat reacts lands the
actual moment in the clip. Set `HAS_DELAY=true` if clips consistently come out too early.

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

**Captions.** With `pip install faster-whisper` and `CAPTIONS=true` (default), each clip is
transcribed on the CPU (about 10-20s for a 30s clip with the `small` model) and the words
are burned into the vertical video as big centred captions, four words at a time. Without
faster-whisper the vertical render simply has no captions.

**Disk budget.** `MAX_CLIPS_GB` / `MAX_CLIPS_KEEP` prune the oldest clips (raw, vertical
and captions together) after every new clip, so the bot can run for weeks unattended.

**Posting.** The default workflow is manual: the Discord webhook pings you, you open
`...-vertical.mp4`, add a hook line in CapCut or TikTok's editor and post. The `score`
and `sample` chat lines in `data/clips.jsonl` tell you which ones to post first.

Automatic TikTok posting exists but is **off by default** (`AUTO_POST=false`). It uses the
MIT-licensed [tiktok-uploader](https://github.com/wkaisertexas/tiktok-uploader) package,
which drives a headless browser with cookies exported from tiktok.com:

```bash
pip install tiktok-uploader && playwright install chromium
# export cookies.txt from tiktok.com with a "Get cookies.txt" browser extension
python -m clipbot post data/clips/<channel>/<file>-vertical.mp4 -d "caption #gta6"
```

Set `AUTO_POST=true` and `TIKTOK_COOKIES=/path/cookies.txt` to post every clip with
`POST_TEMPLATE`. Know the risks: this is against TikTok's terms, accounts get
rate-limited or banned, and TikTok changes its upload page often so the package can
break. Use a dedicated clips account and keep volume human (a handful a day).
YouTube Shorts posting is not built in; [clipmint](https://github.com/kleZ799/clipmint)
has a working YouTube upload if you want one.

## 6. Related open-source projects

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

## 7. Tests

```bash
python -m unittest discover -s tests -v
```

No network needed. The suite covers the detector (bursts, spam, sustain, raids, cooldown,
big chats, reconnect gaps), IRC parsing plus a fake Twitch chat server, the Helix client
(token refresh, 429 / 503 handling, clip polling), the device-code login, the orchestrator
(stream selection, hourly cap, blocked channels, chat-to-clip path), caption grouping and
a real ffmpeg vertical render with burned-in subtitles, retention pruning and posting.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no saved token` | `python -m clipbot auth` |
| `game 'Grand Theft Auto VI' not found` | The category is not live yet; use `CHANNELS` meanwhile |
| `cannot clip <channel> (HTTP 403)` | That streamer restricted clipping; the bot skips them for an hour |
| Clips not downloading | `pip install -U yt-dlp` (Twitch changes its CDN often) |
| No vertical files | Install ffmpeg or set `VERTICAL=false` |
| No captions on clips | `pip install faster-whisper`; first run downloads the model |
| Clips fire on raids / emote waves | Raise `DET_SUSTAIN` to 5; raids are already suppressed |
| Too many / too few clips | See tuning tips above; changes in `.env` apply on restart |

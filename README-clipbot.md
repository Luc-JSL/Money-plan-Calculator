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

## 5. Posting the clips

Posting to TikTok / YouTube is intentionally not automated here: TikTok's API requires an
audited app for public posting, YouTube's quota allows ~6 uploads/day on a new project,
and both platforms down-rank obviously automated accounts. The intended workflow is:
Discord webhook pings you -> open `...-vertical.mp4` -> add a caption / hook text in
CapCut or TikTok's editor -> post. The `score` and `sample` chat lines in
`data/clips.jsonl` tell you which ones to post first.

## 6. Tests

```bash
python -m unittest discover -s tests -v
```

No network needed. The suite covers the detector (bursts, spam, cooldown, big chats,
reconnect gaps), IRC parsing plus a fake Twitch chat server, the Helix client (token
refresh, 429 / 503 handling, clip polling), the device-code login, the orchestrator
(stream selection, hourly cap, blocked channels, chat-to-clip path) and a real ffmpeg
vertical render.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no saved token` | `python -m clipbot auth` |
| `game 'Grand Theft Auto VI' not found` | The category is not live yet; use `CHANNELS` meanwhile |
| `cannot clip <channel> (HTTP 403)` | That streamer restricted clipping; the bot skips them for an hour |
| Clips not downloading | `pip install -U yt-dlp` (Twitch changes its CDN often) |
| No vertical files | Install ffmpeg or set `VERTICAL=false` |
| Too many / too few clips | See tuning tips above; changes in `.env` apply on restart |

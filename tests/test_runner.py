import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from clipbot.config import Config, DetectorConfig
from clipbot.detector import Trigger
from clipbot.runner import Runner
from clipbot.storage import ClipLog, RateLimiter
from clipbot.twitch_api import Clip, HelixError, Stream


def stream(login, viewers, uid=None):
    return Stream(user_id=uid or login + "_id", login=login, display_name=login.title(), game_id="g",
                  game_name="Grand Theft Auto VI", title="playing", viewer_count=viewers, language="en")


class FakeHelix:
    def __init__(self):
        self.created = []
        self.fail_status = None
        self.game_streams = [stream("big", 50000), stream("mid", 9000), stream("small", 100)]
        self.pinned = {"friend": stream("friend", 12)}

    def get_game_id(self, name):
        return "g"

    def get_streams(self, game_id=None, logins=None, languages=None, first=100):
        if logins:
            return [self.pinned[l] for l in logins if l in self.pinned]
        return list(self.game_streams)

    def create_clip(self, broadcaster_id, has_delay=False):
        if self.fail_status:
            raise HelixError(self.fail_status, "nope")
        self.created.append((broadcaster_id, has_delay))
        return f"clip{len(self.created)}", "edit"

    def wait_for_clip(self, clip_id, timeout=30, interval=2):
        return Clip(id=clip_id, url=f"https://clips.twitch.tv/{clip_id}", embed_url="", broadcaster_id="x",
                    broadcaster_name="x", creator_id="me", title="t", thumbnail_url="", created_at="2026-11-19T00:00:00Z",
                    duration=30.0)


def trig(channel):
    return Trigger(channel=channel, ts=time.monotonic(), reason="spike", score=9.5, short_rate=6, baseline_rate=0.5,
                   unique_users=12, hype_fraction=0.8, sample=["LUL", "no way"])


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(client_id="cid", top_streams=2, channels=["friend"], download=False, vertical=False,
                          data_dir=Path(self.tmp.name), max_clips_per_hour=2, detector=DetectorConfig())
        self.helix = FakeHelix()
        self.runner = Runner(self.cfg, helix=self.helix)

    async def test_pick_streams_prefers_pinned_then_top_viewers(self):
        chosen = self.runner.pick_streams(self.helix.game_streams, [self.helix.pinned["friend"]])
        self.assertEqual(list(chosen), ["friend", "big", "mid"])

    async def test_refresh_joins_and_parts_channels(self):
        joined = []
        parted = []

        async def fake_set(wanted):
            joined.append(set(wanted))
            return set(wanted), set()

        self.runner.pool.set_channels = fake_set  # type: ignore[assignment]
        await self.runner.refresh_streams()
        self.assertEqual(joined[-1], {"friend", "big", "mid"})
        self.assertEqual(self.runner.game_id, "g")
        self.helix.game_streams = [stream("newbig", 99999)]
        await self.runner.refresh_streams()
        self.assertEqual(joined[-1], {"friend", "newbig"})
        self.assertNotIn("big", self.runner.streams)

    async def test_trigger_creates_clip_and_logs(self):
        self.runner.streams = {"big": stream("big", 50000)}
        await self.runner.handle_trigger(trig("big"))
        self.assertEqual(self.helix.created, [("big_id", False)])
        recs = ClipLog(self.cfg.log_path).read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].url, "https://clips.twitch.tv/clip1")
        self.assertEqual(recs[0].channel, "big")
        self.assertEqual(recs[0].viewers, 50000)
        self.assertEqual(recs[0].sample, ["LUL", "no way"])

    async def test_unknown_channel_ignored(self):
        await self.runner.handle_trigger(trig("nobody"))
        self.assertEqual(self.helix.created, [])

    async def test_hourly_cap(self):
        self.runner.streams = {c: stream(c, 1) for c in "abc"}
        for c in "abc":
            await self.runner.handle_trigger(trig(c))
        self.assertEqual(len(self.helix.created), 2)

    async def test_403_blocks_channel_for_an_hour(self):
        self.runner.streams = {"big": stream("big", 1)}
        self.helix.fail_status = 403
        await self.runner.handle_trigger(trig("big"))
        self.assertGreater(self.runner.blocked_until["big"], time.monotonic())
        self.helix.fail_status = None
        await self.runner.handle_trigger(trig("big"))
        self.assertEqual(self.helix.created, [])

    async def test_chat_loop_end_to_end(self):
        """Messages flowing through the pool queue reach the detector and produce a clip."""
        self.cfg.detector = DetectorConfig(warmup=0, short_window=10, long_window=20, min_unique_users=3,
                                           min_short_rate=0.5, spike_multiplier=1.0, min_baseline_rate=0.05,
                                           cooldown=100, min_hype_fraction=0.3)
        self.runner = Runner(self.cfg, helix=self.helix)
        self.runner.streams = {"big": stream("big", 1)}
        from clipbot.irc import ChatMessage
        task = asyncio.create_task(self.runner.chat_loop())
        for i in range(8):
            self.runner.pool.queue.put_nowait(ChatMessage("big", f"u{i}", "KEKW no way", "1", 1))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if self.helix.created:
                break
        task.cancel()
        self.assertEqual(len(self.helix.created), 1)


class RateLimiterTests(unittest.TestCase):
    def test_sliding_window(self):
        t = [0.0]
        rl = RateLimiter(2, period=10, clock=lambda: t[0])
        self.assertTrue(rl.allow()); self.assertTrue(rl.allow()); self.assertFalse(rl.allow())
        t[0] = 10.1
        self.assertTrue(rl.allow())


if __name__ == "__main__":
    unittest.main()

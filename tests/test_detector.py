import unittest

from clipbot.config import DetectorConfig
from clipbot.detector import SpikeDetector, is_hype, is_clip_request


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def cfg(**kw):
    base = dict(short_window=10, long_window=120, warmup=60, spike_multiplier=3.0, min_short_rate=1.0,
                min_baseline_rate=0.25, min_unique_users=5, min_hype_fraction=0.35, clip_request_users=3,
                cooldown=120, sustain=0.0)
    base.update(kw)
    return DetectorConfig(**base)


def chatter(det, clock, channel, seconds, rate, text="nice game", hype=False, start_user=0):
    """Emit `rate` msgs/sec for `seconds` seconds from rotating users."""
    out = None
    n = int(seconds * rate)
    for i in range(n):
        clock.t += 1.0 / rate
        user = f"u{(start_user + i) % 40}"
        t = ("KEKW LUL " + text) if hype else text
        r = det.observe(channel, user, t)
        if r:
            out = r
    return out


class HypeTests(unittest.TestCase):
    def test_words_and_phrases(self):
        for t in ["LUL", "kekw", "OMEGALUL", "no way", "CLIP IT", "lmaooo", "what just happened??", "W"]:
            self.assertTrue(is_hype(t), t)

    def test_caps_repeats_punctuation(self):
        self.assertTrue(is_hype("BROOOO"))
        self.assertTrue(is_hype("!!!!"))
        self.assertTrue(is_hype("he actually did it???"))

    def test_emotes_count_as_hype(self):
        self.assertTrue(is_hype("plain text", emote_count=1))

    def test_neutral(self):
        for t in ["what's the fps", "hi everyone", "nice game", "", "   "]:
            self.assertFalse(is_hype(t), t)

    def test_clip_request(self):
        self.assertTrue(is_clip_request("someone CLIP that"))
        self.assertTrue(is_clip_request("clipped"))
        self.assertFalse(is_clip_request("eclipse"))
        self.assertFalse(is_clip_request("paperclips are cool") and False)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1000.0)
        self.det = SpikeDetector(cfg(), clock=self.clock)

    def test_no_trigger_during_warmup(self):
        r = chatter(self.det, self.clock, "a", seconds=30, rate=20, hype=True)
        self.assertIsNone(r)

    def test_steady_chat_never_triggers(self):
        r = chatter(self.det, self.clock, "a", seconds=300, rate=2, hype=True)
        self.assertIsNone(r)

    def test_burst_with_hype_triggers(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)       # baseline 0.5/s
        r = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True)
        self.assertIsNotNone(r)
        self.assertEqual(r.reason, "spike")
        self.assertGreaterEqual(r.unique_users, 5)
        self.assertGreater(r.short_rate, r.baseline_rate * 3)
        self.assertGreater(r.score, 1)
        self.assertEqual(r.channel, "a")

    def test_burst_without_hype_does_not_trigger(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        r = chatter(self.det, self.clock, "a", seconds=10, rate=6, text="gg wp everyone".replace("gg", "good"))
        self.assertIsNone(r)

    def test_single_spammer_does_not_trigger(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        r = None
        for _ in range(80):
            self.clock.t += 0.1
            r = self.det.observe("a", "spammer", "KEKW KEKW KEKW") or r
        self.assertIsNone(r)

    def test_clip_requests_trigger(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        r = None
        for u in ["x", "y", "z"]:
            self.clock.t += 1
            r = self.det.observe("a", u, "CLIP THAT") or r
        self.assertIsNotNone(r)
        self.assertEqual(r.reason, "clip_requests")

    def test_cooldown_blocks_second_trigger(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        r1 = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True)
        self.assertIsNotNone(r1)
        r2 = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True, start_user=5)
        self.assertIsNone(r2)
        chatter(self.det, self.clock, "a", seconds=130, rate=0.5)
        r3 = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True, start_user=10)
        self.assertIsNotNone(r3)

    def test_channels_are_independent(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        chatter(self.det, self.clock, "b", seconds=120, rate=0.5)
        r = chatter(self.det, self.clock, "b", seconds=10, rate=6, hype=True)
        self.assertEqual(r.channel, "b")
        self.assertEqual(self.det.channels["a"].triggers, 0)

    def test_silence_gap_rewarms_instead_of_triggering(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        self.clock.t += 300          # IRC reconnect / stream restart
        r = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True)
        self.assertIsNone(r)

    def test_forget_resets_warmup(self):
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)
        self.det.forget("a")
        r = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True)
        self.assertIsNone(r)

    def test_big_chat_needs_proportional_spike(self):
        # 20 msgs/s baseline (big streamer): 30 msgs/s is not a spike, 80 is.
        chatter(self.det, self.clock, "big", seconds=120, rate=20, hype=True)
        self.assertIsNone(chatter(self.det, self.clock, "big", seconds=10, rate=30, hype=True))
        chatter(self.det, self.clock, "big", seconds=125, rate=20, hype=True)
        self.assertIsNotNone(chatter(self.det, self.clock, "big", seconds=10, rate=80, hype=True))

    def test_env_config_roundtrip(self):
        import os
        os.environ["DET_COOLDOWN"] = "33"
        try:
            self.assertEqual(DetectorConfig.from_env().cooldown, 33)
        finally:
            del os.environ["DET_COOLDOWN"]


if __name__ == "__main__":
    unittest.main()

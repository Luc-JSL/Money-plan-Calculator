"""Tests for the features adopted from other open-source clippers:
sustain, raid suppression, captions, retention and TikTok posting."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from clipbot import captions, post, retention
from clipbot.captions import CaptionLine, group_words, write_ass
from clipbot.config import DetectorConfig
from clipbot.detector import SpikeDetector
from clipbot.irc import parse_line
from clipbot.media import have_ffmpeg, render_vertical
from tests.test_detector import FakeClock, chatter, cfg


class SustainTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1000.0)
        self.det = SpikeDetector(cfg(sustain=3.0), clock=self.clock)
        chatter(self.det, self.clock, "a", seconds=120, rate=0.5)

    def test_short_wave_does_not_trigger(self):
        r = chatter(self.det, self.clock, "a", seconds=2.5, rate=6, hype=True)
        self.assertIsNone(r)
        self.assertIsNotNone(self.det.channels["a"].hot_since)

    def test_sustained_spike_triggers_after_sustain(self):
        r = chatter(self.det, self.clock, "a", seconds=10, rate=6, hype=True)
        self.assertIsNotNone(r)
        start = self.det.channels["a"].first_seen + 120
        self.assertGreaterEqual(r.ts - start, 2.5)   # sustain counts from when the burst got hot
        self.assertLess(r.ts - start, 6.0)

    def test_collapse_resets_streak(self):
        chatter(self.det, self.clock, "a", seconds=2, rate=6, hype=True)
        self.clock.t += 12                      # chat goes quiet: short window empties
        self.det.observe("a", "u1", "hi")
        self.assertIsNone(self.det.channels["a"].hot_since)

    def test_clip_requests_bypass_sustain(self):
        r = None
        for u in "xyz":
            self.clock.t += 0.5
            r = self.det.observe("a", u, "CLIP IT") or r
        self.assertEqual(r.reason, "clip_requests")


class RaidTests(unittest.TestCase):
    def test_parse_raid_usernotice(self):
        line = ("@badge-info=;badges=;color=;display-name=Raider;emotes=;id=1;login=raider;mod=0;msg-id=raid;"
                "msg-param-displayName=Raider;msg-param-login=raider;msg-param-viewerCount=1500;room-id=2;"
                "subscriber=0;system-msg=1500\\sraiders\\sfrom\\sRaider\\shave\\sjoined!;tmi-sent-ts=1;user-id=3;"
                "user-type= :tmi.twitch.tv USERNOTICE #target")
        m = parse_line(line)
        self.assertEqual(m.kind, "raid")
        self.assertEqual(m.channel, "target")
        self.assertEqual(m.user, "raider")
        self.assertIn("1500 raiders", m.text)

    def test_other_usernotice_ignored(self):
        self.assertIsNone(parse_line("@msg-id=sub;login=x :tmi.twitch.tv USERNOTICE #c :hi"))

    def test_suppress_blocks_trigger(self):
        clock = FakeClock(1000.0)
        det = SpikeDetector(cfg(), clock=clock)
        chatter(det, clock, "a", seconds=120, rate=0.5)
        det.suppress("a", 120)
        self.assertIsNone(chatter(det, clock, "a", seconds=10, rate=6, hype=True))
        chatter(det, clock, "a", seconds=115, rate=0.5)
        self.assertIsNotNone(chatter(det, clock, "a", seconds=10, rate=6, hype=True, start_user=7))


class CaptionTests(unittest.TestCase):
    WORDS = [(0.0, 0.3, "this"), (0.3, 0.5, "is"), (0.5, 0.9, "insane"), (0.9, 1.2, "bro"), (1.2, 1.5, "no"),
             (3.0, 3.4, "way"), (3.4, 3.8, "he"), (3.8, 4.4, "did"), (4.4, 5.0, "that"), (5.0, 7.0, "unbelievable")]

    def test_group_words(self):
        lines = group_words(self.WORDS, max_words=4, max_gap=0.8, max_span=2.5)
        texts = [l.text for l in lines]
        self.assertEqual(texts, ["this is insane bro", "no", "way he did that", "unbelievable"])
        self.assertEqual(lines[0].start, 0.0)
        self.assertAlmostEqual(lines[1].end, 1.5)
        self.assertEqual(lines[2].start, 3.0)

    def test_write_ass_escapes_and_formats(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_ass([CaptionLine(0.5, 2.25, "hey {there}\\ fam"), CaptionLine(3, 3, "skip me")], Path(d) / "c.ass")
            text = p.read_text()
            self.assertIn("Dialogue: 0,0:00:00.50,0:00:02.25,Cap,,0,0,0,,HEY THERE FAM", text)
            self.assertNotIn("skip", text)
            self.assertIn("PlayResY: 1920", text)

    def test_make_captions_with_injected_transcriber(self):
        with tempfile.TemporaryDirectory() as d:
            out = captions.make_captions(Path(d) / "v.mp4", Path(d) / "v.ass", transcriber=lambda p: iter(self.WORDS))
            self.assertTrue(out.is_file())
            self.assertIsNone(captions.make_captions(Path(d) / "v.mp4", Path(d) / "e.ass", transcriber=lambda p: iter([])))
            self.assertIsNone(captions.make_captions(Path(d) / "v.mp4", Path(d) / "x.ass",
                                                     transcriber=lambda p: (_ for _ in ()).throw(RuntimeError("boom"))))

    @unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
    def test_burn_in_with_real_ffmpeg(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                            "-pix_fmt", "yuv420p", str(src)], check=True)
            ass = write_ass([CaptionLine(0.0, 1.0, "CAPTION TEST")], Path(d) / "20260101-000000-abc.ass")
            out = render_vertical(src, Path(d) / "out.mp4", ass_path=ass)
            self.assertIsNotNone(out)
            self.assertGreater(out.stat().st_size, 1000)


class RetentionTests(unittest.TestCase):
    def _mk(self, root, stamp, size, age):
        for suffix in (".mp4", "-vertical.mp4", ".ass"):
            p = root / "chan" / f"{stamp}{suffix}"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)
            os.utime(p, (time.time() - age, time.time() - age))

    def test_prunes_oldest_groups_whole(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._mk(root, "20260101-000000-old", 1000, 300)
            self._mk(root, "20260101-000100-mid", 1000, 200)
            self._mk(root, "20260101-000200-new", 1000, 100)
            (root / "chan" / "keep-me.mp4").write_bytes(b"y" * 5000)   # not a clip: never touched
            self.assertEqual(retention.usage(root), (9000, 3))
            freed, deleted = retention.enforce(root, max_gb=0, max_clips=1)
            self.assertEqual((freed, deleted), (6000, 2))
            left = sorted(p.name for p in (root / "chan").iterdir())
            self.assertEqual(left, ["20260101-000200-new-vertical.mp4", "20260101-000200-new.ass",
                                    "20260101-000200-new.mp4", "keep-me.mp4"])

    def test_size_cap_and_always_keep_one(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._mk(root, "20260101-000000-a", 10_000, 50)
            self._mk(root, "20260101-000001-b", 10_000, 10)
            freed, deleted = retention.enforce(root, max_gb=1e-9, max_clips=0)
            self.assertEqual(deleted, 1)
            self.assertEqual(retention.usage(root)[1], 1)

    def test_disabled_and_missing(self):
        self.assertEqual(retention.enforce(Path("/nonexistent"), 1, 1), (0, 0))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(retention.enforce(Path(d), 0, 0), (0, 0))


class PostTests(unittest.TestCase):
    def test_post_with_fake_uploader(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "v.mp4"; v.write_bytes(b"0")
            ck = Path(d) / "cookies.txt"; ck.write_text("# Netscape HTTP Cookie File\n")
            ok = post.post_tiktok(v, "desc #gta6", str(ck), uploader=lambda *a, **k: calls.append((a, k)) or [])
            self.assertTrue(ok)
            self.assertEqual(calls[0][0], (str(v),))
            self.assertEqual(calls[0][1]["description"], "desc #gta6")
            self.assertFalse(post.post_tiktok(v, "x", str(ck), uploader=lambda *a, **k: [{"path": str(v)}]))
            self.assertFalse(post.post_tiktok(v, "x", str(ck), uploader=lambda *a, **k: (_ for _ in ()).throw(RuntimeError())))
            self.assertFalse(post.post_tiktok(v, "x", "", uploader=lambda *a, **k: []))
            self.assertFalse(post.post_tiktok(Path(d) / "missing.mp4", "x", str(ck), uploader=lambda *a, **k: []))

    def test_config_validation_for_auto_post(self):
        from clipbot.config import Config
        msgs = "\n".join(Config(client_id="x", auto_post=["tiktok"], tiktok_cookies="", vertical=False).validate())
        self.assertIn("TIKTOK_COOKIES", msgs)
        self.assertIn("VERTICAL", msgs)


if __name__ == "__main__":
    unittest.main()

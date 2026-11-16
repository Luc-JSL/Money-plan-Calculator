import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from clipbot.config import Config, load_dotenv
from clipbot.media import have_ffmpeg, render_vertical, safe_name, vertical_filter


class ConfigTests(unittest.TestCase):
    def test_dotenv_and_validate(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# comment\nTWITCH_CLIENT_ID="abc"\nCHANNELS=#Foo, bar ,\nTOP_STREAMS=5\nVERTICAL=no\n')
            for k in ["TWITCH_CLIENT_ID", "CHANNELS", "TOP_STREAMS", "VERTICAL"]:
                os.environ.pop(k, None)
            load_dotenv(p)
            cfg = Config.from_env()
            self.assertEqual(cfg.client_id, "abc")
            self.assertEqual(cfg.channels, ["foo", "bar"])
            self.assertEqual(cfg.top_streams, 5)
            self.assertFalse(cfg.vertical)
            self.assertEqual(cfg.validate(), [])
            for k in ["TWITCH_CLIENT_ID", "CHANNELS", "TOP_STREAMS", "VERTICAL"]:
                os.environ.pop(k, None)

    def test_validate_catches_problems(self):
        cfg = Config(client_id="", top_streams=0)
        msgs = "\n".join(cfg.validate())
        self.assertIn("TWITCH_CLIENT_ID", msgs)
        self.assertIn("Nothing to watch", msgs)


class MediaTests(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(safe_name("Some Clip/../x?"), "Some_Clip_.._x")
        self.assertEqual(safe_name("///"), "clip")

    def test_vertical_filter_shape(self):
        f = vertical_filter()
        self.assertIn("scale=1080:1920", f)
        self.assertIn("overlay", f)

    @unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
    def test_render_vertical_real_ffmpeg(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                            "-pix_fmt", "yuv420p", str(src)], check=True)
            out = render_vertical(src, Path(d) / "out.mp4")
            self.assertIsNotNone(out)
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
            self.assertEqual(probe.stdout.strip(), "1080,1920")


if __name__ == "__main__":
    unittest.main()

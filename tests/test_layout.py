import subprocess
import tempfile
import unittest
from pathlib import Path

from clipbot import layout
from clipbot.layout import (choose_graph, find_facecam, inset_from_face, looks_like_inset, parse_box, split_graph,
                            stable_box, stack_graph)
from clipbot.media import have_ffmpeg, render_vertical

FW, FH = 1920, 1080
CORNER_FACE = (1620, 80, 120, 150)      # small face top-right: a webcam inset
CENTRE_FACE = (900, 400, 160, 200)      # face in the middle: a game character / IRL shot
BIG_FACE = (1500, 100, 400, 500)        # too big to be an inset


class GeometryTests(unittest.TestCase):
    def test_looks_like_inset(self):
        self.assertTrue(looks_like_inset(CORNER_FACE, FW, FH))
        self.assertFalse(looks_like_inset(CENTRE_FACE, FW, FH))
        self.assertFalse(looks_like_inset(BIG_FACE, FW, FH))
        self.assertTrue(looks_like_inset((40, 820, 120, 150), FW, FH))     # bottom-left

    def test_inset_from_face_is_16_9_inside_frame(self):
        x, y, w, h = inset_from_face(CORNER_FACE, FW, FH)
        self.assertAlmostEqual(w / h, 16 / 9, delta=0.02)
        self.assertEqual(w, 360)
        self.assertGreaterEqual(x, 0); self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, FW); self.assertLessEqual(y + h, FH)

    def test_stable_box_rejects_moving_subject(self):
        pinned = [inset_from_face((1620 + d, 80 + d, 120, 150), FW, FH) for d in (0, 3, -2, 5, 1)]
        self.assertIsNotNone(stable_box(pinned, FW, FH))
        moving = [inset_from_face((1620 - 150 * i, 80 + 60 * i, 120, 150), FW, FH) for i in range(5)]
        self.assertIsNone(stable_box(moving, FW, FH))
        self.assertIsNone(stable_box(pinned[:2], FW, FH))

    def test_parse_box(self):
        self.assertEqual(parse_box("1500, 40, 400,225"), (1500, 40, 400, 225))
        self.assertIsNone(parse_box("1,2,3"))
        self.assertIsNone(parse_box("a,b,c,d"))
        self.assertIsNone(parse_box("1,2,0,5"))
        self.assertIsNone(parse_box(""))


class GraphTests(unittest.TestCase):
    def test_split_shares_height(self):
        g = split_graph((1500, 40, 400, 225), facecam_share=0.38)
        self.assertIn("crop=400:224:1500:40", g)            # even-sized crop
        self.assertIn("scale=1080:730", g)                   # 38% of 1920, even
        self.assertIn("scale=1080:1190", g)                  # the rest for the game
        self.assertIn("vstack", g)
        g2 = split_graph((0, 0, 400, 225), facecam_share=0.9)   # clamped to 50%
        self.assertIn("scale=1080:960", g2)

    def test_stack_and_choose(self):
        g = stack_graph((1500, 40, 400, 225))
        self.assertIn("boxblur", g); self.assertIn("vstack", g); self.assertIn("scale=1080:608", g)
        self.assertEqual(choose_graph("auto", None)[1], "blurred")
        self.assertEqual(choose_graph("auto", (1, 2, 30, 40))[1], "split")
        self.assertEqual(choose_graph("stack", (1, 2, 30, 40))[1], "stack")
        self.assertEqual(choose_graph("blurred", (1, 2, 30, 40))[1], "blurred")
        self.assertEqual(choose_graph("split", None)[1], "blurred")


def make_video(path: Path, seconds: int = 2, size: str = "640x360"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def has_cv2():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
class RenderTests(unittest.TestCase):
    def _size(self, p):
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout.strip()

    def test_split_and_stack_render_1080x1920(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.mp4"
            make_video(src)
            box = (480, 20, 150, 84)
            for name, graph in [("split", split_graph(box)), ("stack", stack_graph(box))]:
                out = render_vertical(src, Path(d) / f"{name}.mp4", graph=graph)
                self.assertIsNotNone(out, name)
                self.assertEqual(self._size(out), "1080,1920", name)

    @unittest.skipUnless(has_cv2(), "opencv not installed")
    def test_find_facecam_with_fake_detector(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.mp4"
            make_video(src, seconds=3, size="1920x1080")
            calls = []

            def pinned(frame):
                calls.append(frame.shape)
                return [CENTRE_FACE, CORNER_FACE]          # the big centre face is ignored

            box = find_facecam(src, pinned, samples=6)
            self.assertIsNotNone(box)
            self.assertEqual(len(calls), 6)
            self.assertEqual(calls[0][:2], (1080, 1920))
            x, y, w, h = box
            self.assertGreater(x, 1400); self.assertLess(y, 200)
            self.assertIsNone(find_facecam(src, lambda f: [], samples=6))
            self.assertIsNone(find_facecam(src, None))
            self.assertIsNone(layout.yunet_detector(Path(d) / "missing.onnx"))


if __name__ == "__main__":
    unittest.main()

import asyncio
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from clipbot import recorder
from clipbot.recorder import (SEGMENT_SECONDS, Recorder, Segment, concat_segments, have_streamlink,
                              list_segments, select_segments)
from clipbot.media import have_ffmpeg


def seg(name, end, size=10):
    return Segment(Path(name), end, size)


class SelectTests(unittest.TestCase):
    def test_select_window(self):
        segs = [seg(f"{i}.ts", 1000 + 2 * i) for i in range(1, 21)]      # ends at 1002..1040
        for a, b in zip(segs, segs[1:]):
            b.start = a.end
        segs[0].start = segs[0].end - 2
        chosen = select_segments(segs, trigger_ts=1030, pre=10, post=0)
        self.assertEqual([s.end for s in chosen], [1022, 1024, 1026, 1028, 1030])
        chosen = select_segments(segs, trigger_ts=1030, pre=4, post=3)
        self.assertEqual([s.end for s in chosen], [1028, 1030, 1032, 1034])
        self.assertEqual(select_segments(segs, trigger_ts=2000, pre=10, post=0), [])

    def test_list_segments_skips_empty_and_sorts(self):
        with tempfile.TemporaryDirectory() as d:
            for i, size in [(3, 5), (1, 5), (2, 0)]:
                p = Path(d) / f"{i:08d}.ts"
                p.write_bytes(b"x" * size)
                os.utime(p, (1000 + i, 1000 + i))
            segs = list_segments(Path(d))
            self.assertEqual([s.path.name for s in segs], ["00000001.ts", "00000003.ts"])
            self.assertEqual(segs[1].start, segs[0].end)
            self.assertEqual(segs[0].start, segs[0].end - SEGMENT_SECONDS)


@unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
class ConcatTests(unittest.TestCase):
    def test_concat_real_segments(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "10", "-c:v", "libx264", "-g", "60",
                            "-force_key_frames", "expr:gte(t,n_forced*2)",
                            "-c:a", "aac", "-pix_fmt", "yuv420p", "-f", "segment", "-segment_time", "2",
                            "-segment_format", "mpegts", "-reset_timestamps", "1", str(d / "%08d.ts")], check=True)
            segs = list_segments(d)
            self.assertGreaterEqual(len(segs), 3)
            out = concat_segments(segs[:3], d / "clip.mp4")
            self.assertIsNotNone(out)
            dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                  str(out)], capture_output=True, text=True).stdout.strip()
            self.assertGreaterEqual(float(dur), 5.0)
            self.assertFalse((d / "clip.txt").exists())


def _serve(directory: str):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(have_ffmpeg() and have_streamlink(), "ffmpeg + streamlink needed")
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """A local live HLS stream (ffmpeg -> http.server) stands in for Twitch; streamlink
    pulls it with the generic hls:// plugin exactly as it would pull twitch.tv."""

    async def test_buffer_then_cut_30_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            hls = d / "hls"; hls.mkdir()
            src = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
                                    "testsrc=size=320x180:rate=30", "-f", "lavfi", "-i", "sine=frequency=440",
                                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac",
                                    "-pix_fmt", "yuv420p", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
                                    "-hls_flags", "delete_segments", str(hls / "live.m3u8")])
            srv = _serve(str(hls))
            port = srv.server_address[1]
            rec = Recorder("test", d / "buf", buffer_seconds=20, quality="best",
                           source_url=f"hls://http://127.0.0.1:{port}/live.m3u8")
            try:
                for _ in range(60):          # wait for the playlist to appear
                    if (hls / "live.m3u8").exists():
                        break
                    await asyncio.sleep(0.5)
                await rec.start()
                for _ in range(120):         # wait until ~8s of video is buffered
                    segs = list_segments(d / "buf")
                    if len(segs) >= 4:
                        break
                    await asyncio.sleep(0.5)
                self.assertTrue(rec.is_live(), "recorder never produced segments")
                trigger = time.time()
                clip = await rec.cut(trigger, pre=6, post=0, dest=d / "clip.mp4")
                self.assertIsNotNone(clip)
                dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                            "csv=p=0", str(clip)], capture_output=True, text=True).stdout.strip())
                self.assertGreaterEqual(dur, 4.0)
                self.assertLessEqual(dur, 6.0 + 2 * SEGMENT_SECONDS + 1)
                # prune keeps the buffer bounded
                await asyncio.sleep(1)
                rec.prune()
                self.assertLessEqual(len(list_segments(d / "buf")) * SEGMENT_SECONDS, 20 + 2 * SEGMENT_SECONDS)
            finally:
                await rec.stop()
                src.terminate(); src.wait(timeout=5)
                srv.shutdown()


if __name__ == "__main__":
    unittest.main()


class EncoderFallbackTests(unittest.TestCase):
    def test_falls_back_to_libx264_when_hardware_encoder_fails(self):
        from clipbot import media
        media._ENCODERS, media._FAILED = ["h264_nvenc", "libx264"], set()
        try:
            used = []

            def build(venc):
                used.append(venc[1])
                return ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5",
                        "-t", "0.4", *venc, "-f", "null", "-"]

            ok = media.run_encode(build, 60, "test")
            self.assertTrue(ok)
            self.assertEqual(used[-1], "libx264")
            if used[0] == "h264_nvenc":                 # no GPU here: nvenc failed and was blacklisted
                self.assertIn("h264_nvenc", media._FAILED)
                self.assertEqual(media.available_encoders(), ["libx264"])
        finally:
            media._ENCODERS, media._FAILED = None, set()

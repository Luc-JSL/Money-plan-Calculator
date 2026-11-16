import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from clipbot import youtube as yt
from clipbot.config import Config
from clipbot.post import PostState, Poster
from clipbot.storage import ClipRecord
from clipbot.youtube import GoogleTokenStore, GoogleTokens, YouTube, YouTubeError
from tests.test_api import FakeHTTP


class FakeYouTube:
    def __init__(self):
        self.calls = []
        self.fail = False

    def upload(self, video, title, description="", tags=None, privacy="public", category_id="20"):
        if self.fail:
            raise YouTubeError("quota")
        self.calls.append((Path(video).name, title, description, privacy))
        return f"https://youtube.com/shorts/v{len(self.calls)}"


def mkcfg(tmp, **kw):
    base = dict(client_id="", clip_mode="local", channels=["a"], data_dir=Path(tmp), auto_post=["youtube"],
                max_posts_per_day=2, youtube_client_id="cid", youtube_client_secret="sec")
    base.update(kw)
    return Config(**base)


class PosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = mkcfg(self.tmp.name)
        self.yt = FakeYouTube()
        self.poster = Poster(self.cfg, youtube=self.yt)
        self.videos = []
        for i in range(4):
            v = Path(self.tmp.name) / f"c{i}-vertical.mp4"
            v.write_bytes(b"v")
            self.videos.append(v)

    def test_daily_cap_and_no_double_post(self):
        r1 = self.poster.post(self.videos[0], "youtube", "cap #x", "title one")
        self.assertTrue(r1.ok)
        self.assertEqual(r1.url, "https://youtube.com/shorts/v1")
        self.assertEqual(self.poster.post(self.videos[0], "youtube", "cap", "t").skipped, "already")
        self.assertTrue(self.poster.post(self.videos[1], "youtube", "cap", "t").ok)
        r3 = self.poster.post(self.videos[2], "youtube", "cap", "t")
        self.assertEqual(r3.skipped, "cap")
        self.assertEqual(len(self.yt.calls), 2)
        self.assertTrue(self.poster.post(self.videos[2], "youtube", "cap", "t", force=True).ok)
        # state persists across instances
        state = PostState(Path(self.tmp.name) / "post_state.json")
        self.assertTrue(state.is_posted(self.videos[0], "youtube"))
        self.assertEqual(state.count_today("youtube"), 3)

    def test_failure_is_not_recorded(self):
        self.yt.fail = True
        r = self.poster.post(self.videos[0], "youtube", "cap", "t")
        self.assertFalse(r.ok)
        self.assertFalse(self.poster.state.is_posted(self.videos[0], "youtube"))

    def test_post_all_and_tiktok_injection(self):
        calls = []
        ck = Path(self.tmp.name) / "cookies.txt"; ck.write_text("#")
        cfg = mkcfg(self.tmp.name, auto_post=["youtube", "tiktok"], tiktok_cookies=str(ck))
        poster = Poster(cfg, youtube=self.yt, tiktok_uploader=lambda *a, **k: calls.append(k) or [])
        results = poster.post_all(self.videos[0], "hello #gta6", "T")
        self.assertEqual([(r.platform, r.ok) for r in results], [("youtube", True), ("tiktok", True)])
        self.assertEqual(calls[0]["description"], "hello #gta6")

    def test_post_pending_prefers_high_scores_and_respects_cap(self):
        recs = []
        for i, score in enumerate([3.0, 9.0, 5.0, 7.0]):
            recs.append(ClipRecord(clip_id=str(i), url="", channel="xqc", broadcaster_id="", game="GTA VI",
                                   created_at="", reason="spike", score=score, short_rate=1, baseline_rate=1,
                                   unique_users=5, hype_fraction=0.5, vertical_file=str(self.videos[i])))
        recs.append(ClipRecord(clip_id="m", url="", channel="x", broadcaster_id="", game="", created_at="", reason="s",
                               score=99, short_rate=1, baseline_rate=1, unique_users=1, hype_fraction=1,
                               vertical_file="/nonexistent.mp4"))
        results = self.poster.post_pending(recs)
        self.assertEqual([r.ok for r in results], [True, True])
        self.assertEqual([c[0] for c in self.yt.calls], ["c1-vertical.mp4", "c3-vertical.mp4"])
        self.assertIn("xqc", self.yt.calls[0][1])
        self.assertEqual(self.poster.post_pending(recs), [])   # cap reached today

    def test_config_parsing_and_validation(self):
        import os
        os.environ["AUTO_POST"] = "youtube, tiktok"
        try:
            self.assertEqual(Config.from_env().auto_post, ["youtube", "tiktok"])
            os.environ["AUTO_POST"] = "true"
            self.assertEqual(Config.from_env().auto_post, ["tiktok"])
            os.environ["AUTO_POST"] = "false"
            self.assertEqual(Config.from_env().auto_post, [])
        finally:
            del os.environ["AUTO_POST"]
        msgs = "\n".join(mkcfg(self.tmp.name, auto_post=["youtube", "bogus"], youtube_client_id="").validate())
        self.assertIn("YOUTUBE_CLIENT_ID", msgs)
        self.assertIn("bogus", msgs)
        self.assertEqual(mkcfg(self.tmp.name).validate(), [])


class YouTubeClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeHTTP()
        patcher = mock.patch.object(yt.h, "request", self.fake)
        patcher.start(); self.addCleanup(patcher.stop)
        self.store = GoogleTokenStore(Path(self.tmp.name) / "yt.json")
        self.store.save(GoogleTokens("acc", "ref", time.time() + 3600))
        self.video = Path(self.tmp.name) / "v.mp4"
        self.video.write_bytes(b"x" * 1000)

    def _patch_put(self, responses):
        """Fake urlopen for the PUT chunks: list of (status, body)."""
        seen = []

        class Resp:
            def __init__(self, status, body): self.status, self._b = status, body
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            seen.append((req.get_method(), req.headers.get("Content-range"), len(req.data)))
            status, body = responses.pop(0)
            if status >= 400:
                raise urllib.error.HTTPError(req.full_url, status, "err", {}, None)
            return Resp(status, body)

        return mock.patch.object(yt.urllib.request, "urlopen", fake_urlopen), seen

    def test_resumable_upload_single_chunk(self):
        self.fake.add(200, "", {"location": "https://upload.example/session"})
        client = YouTube("cid", "sec", self.store)
        p, seen = self._patch_put([(200, b'{"id": "abc123"}')])
        with p:
            url = client.upload(self.video, "T" * 120, "desc", tags=["a"], privacy="unlisted")
        self.assertEqual(url, "https://youtube.com/shorts/abc123")
        m, u, params, headers, body, _ = self.fake.calls[0]
        self.assertEqual((m, u), ("POST", yt.UPLOAD_URL))
        self.assertEqual(params["uploadType"], "resumable")
        self.assertEqual(headers["X-Upload-Content-Length"], "1000")
        self.assertEqual(len(body["snippet"]["title"]), 100)
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")
        self.assertEqual(seen, [("PUT", "bytes 0-999/1000", 1000)])

    def test_multi_chunk_with_308(self):
        self.fake.add(200, "", {"location": "https://upload.example/session"})
        client = YouTube("cid", "sec", self.store)
        with mock.patch.object(yt, "CHUNK", 600):
            p, seen = self._patch_put([(308, b""), (201, b'{"id": "zz"}')])
            with p:
                url = client.upload(self.video, "t")
        self.assertEqual(url, "https://youtube.com/shorts/zz")
        self.assertEqual([s[1] for s in seen], ["bytes 0-599/1000", "bytes 600-999/1000"])

    def test_refresh_when_expired_and_session_error(self):
        self.store.save(GoogleTokens("old", "ref", time.time() - 1))
        self.fake.add(200, {"access_token": "new", "expires_in": 3600})
        self.fake.add(403, {"error": {"message": "quotaExceeded"}})
        client = YouTube("cid", "sec", self.store)
        with self.assertRaises(YouTubeError) as ctx:
            client.upload(self.video, "t")
        self.assertIn("quotaExceeded", str(ctx.exception))
        self.assertEqual(self.fake.calls[0][5]["grant_type"], "refresh_token")
        self.assertEqual(self.fake.calls[1][3]["Authorization"], "Bearer new")
        self.assertEqual(self.store.load().access_token, "new")

    def test_not_connected(self):
        client = YouTube("cid", "sec", GoogleTokenStore(Path(self.tmp.name) / "none.json"))
        with self.assertRaises(YouTubeError):
            client.upload(self.video, "t")

    def test_loopback_login(self):
        import threading, urllib.request as ur
        self.fake.add(200, {"access_token": "a", "refresh_token": "r", "expires_in": 100})
        out = []
        store = GoogleTokenStore(Path(self.tmp.name) / "login.json")
        result = {}

        def run():
            result["t"] = yt.interactive_login("cid", "sec", store, print_fn=out.append, open_browser=False, timeout=10)

        th = threading.Thread(target=run); th.start()
        for _ in range(50):
            time.sleep(0.05)
            if out:
                break
        url = next(l.strip() for l in out[0].split("\n") if l.strip().startswith("https://"))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(q["scope"], yt.SCOPE)
        redirect = q["redirect_uri"]
        with self.assertRaises(urllib.error.HTTPError):          # wrong state is rejected
            ur.urlopen(f"{redirect}/?state=wrong&code=x")
        ur.urlopen(f"{redirect}/?state={q['state']}&code=thecode").read()
        th.join(5)
        self.assertEqual(result["t"].refresh_token, "r")
        self.assertEqual(self.fake.calls[-1][5]["code"], "thecode")
        self.assertTrue(store.path.is_file())


import urllib.parse  # noqa: E402  (used in the login test)

if __name__ == "__main__":
    unittest.main()

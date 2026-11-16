import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipbot import auth, http, twitch_api
from clipbot.auth import AuthError, TokenManager, TokenStore, Tokens
from clipbot.twitch_api import Helix, HelixError


class FakeHTTP:
    """Scripted responses keyed by (METHOD, path-prefix); records calls."""

    def __init__(self):
        self.calls = []
        self.script = []

    def add(self, status, body, headers=None):
        self.script.append(http.Response(status, body, headers or {}))

    def __call__(self, method, url, params=None, headers=None, json_body=None, form=None, timeout=20.0):
        self.calls.append((method, url, params, headers, json_body, form))
        if not self.script:
            raise AssertionError(f"unexpected request {method} {url}")
        return self.script.pop(0)


def make_tm(tmp, expires_in=3600):
    store = TokenStore(Path(tmp) / "tokens.json")
    store.save(Tokens("acc", "ref", time.time() + expires_in, ["clips:edit"], "1", "me"))
    return TokenManager("cid", store)


class HelixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeHTTP()
        patcher = mock.patch.object(twitch_api.http, "request", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.sleeps = []
        self.helix = Helix("cid", make_tm(self.tmp.name), sleep=self.sleeps.append)

    def test_headers_and_create_clip(self):
        self.fake.add(202, {"data": [{"id": "ClipX", "edit_url": "https://clips.twitch.tv/ClipX/edit"}]})
        cid, edit = self.helix.create_clip("123", has_delay=True)
        self.assertEqual(cid, "ClipX")
        m, url, params, headers, *_ = self.fake.calls[0]
        self.assertEqual((m, url), ("POST", "https://api.twitch.tv/helix/clips"))
        self.assertEqual(params, {"broadcaster_id": "123", "has_delay": "true"})
        self.assertEqual(headers["Client-Id"], "cid")
        self.assertEqual(headers["Authorization"], "Bearer acc")

    def test_401_refreshes_token_and_retries(self):
        self.fake.add(401, {"message": "Invalid OAuth token"})
        self.fake.add(200, {"data": [{"id": "x"}]})
        with mock.patch.object(auth, "refresh", return_value=Tokens("new", "ref2", time.time() + 100, ["clips:edit"])):
            self.helix.get_game_id("Grand Theft Auto VI")
        self.assertEqual(self.fake.calls[1][3]["Authorization"], "Bearer new")
        saved = json.loads((Path(self.tmp.name) / "tokens.json").read_text())
        self.assertEqual(saved["access_token"], "new")

    def test_429_waits_then_retries(self):
        self.fake.add(429, {"message": "Too Many Requests"}, {"ratelimit-reset": str(time.time() + 3)})
        self.fake.add(200, {"data": []})
        self.assertIsNone(self.helix.get_game_id("nothing"))
        self.assertEqual(len(self.sleeps), 1)
        self.assertGreaterEqual(self.sleeps[0], 1.0)

    def test_5xx_retries_then_raises(self):
        for _ in range(4):
            self.fake.add(503, {"message": "Service Unavailable"})
        with self.assertRaises(HelixError) as ctx:
            self.helix.create_clip("1")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(len(self.fake.calls), 4)

    def test_403_raises_immediately(self):
        self.fake.add(403, {"message": "The broadcaster has restricted the ability to capture clips"})
        with self.assertRaises(HelixError) as ctx:
            self.helix.create_clip("1")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(self.fake.calls), 1)

    def test_get_streams_params_and_parsing(self):
        self.fake.add(200, {"data": [{"user_id": "9", "user_login": "BigGuy", "user_name": "BigGuy", "game_id": "g",
                                      "game_name": "GTA", "title": "t", "viewer_count": "1234", "language": "en"}]})
        streams = self.helix.get_streams(game_id="g", languages=["en", "es"], first=500)
        params = self.fake.calls[0][2]
        self.assertEqual(params, {"type": "live", "first": 100, "game_id": "g", "language": ["en", "es"]})
        self.assertEqual(streams[0].login, "bigguy")
        self.assertEqual(streams[0].viewer_count, 1234)

    def test_wait_for_clip_polls(self):
        self.fake.add(200, {"data": []})
        self.fake.add(200, {"data": []})
        self.fake.add(200, {"data": [{"id": "c", "url": "https://clips.twitch.tv/c", "duration": "30.1"}]})
        clip = self.helix.wait_for_clip("c", timeout=60, interval=2)
        self.assertEqual(clip.url, "https://clips.twitch.tv/c")
        self.assertEqual(clip.duration, 30.1)
        self.assertEqual(self.sleeps, [2, 2])

    def test_expired_token_refreshed_before_call(self):
        helix = Helix("cid", make_tm(self.tmp.name, expires_in=10))
        self.fake.add(200, {"data": []})
        with mock.patch.object(auth, "refresh", return_value=Tokens("fresh", "r", time.time() + 100, ["clips:edit"])) as r:
            helix.get_game_id("x")
        r.assert_called_once()
        self.assertEqual(self.fake.calls[0][3]["Authorization"], "Bearer fresh")


class DeviceFlowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHTTP()
        patcher = mock.patch.object(auth.http, "request", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_full_flow(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fake.add(200, {"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://www.twitch.tv/activate",
                            "expires_in": 1800, "interval": 5})
        self.fake.add(400, {"status": 400, "message": "authorization_pending"})
        self.fake.add(200, {"access_token": "a", "refresh_token": "r", "expires_in": 14000, "scope": ["clips:edit"],
                            "token_type": "bearer"})
        self.fake.add(200, {"login": "me", "user_id": "77", "scopes": ["clips:edit"], "expires_in": 14000})
        out, sleeps = [], []
        tm = TokenManager("cid", TokenStore(Path(tmp.name) / "t.json"))
        tokens = tm.interactive_login(print_fn=out.append, sleep=sleeps.append)
        self.assertEqual(tokens.login, "me")
        self.assertEqual(sleeps, [5.0])
        self.assertIn("Code: ABCD", "\n".join(out))
        form = self.fake.calls[0][5]
        self.assertEqual(form, {"client_id": "cid", "scopes": "clips:edit"})
        poll = self.fake.calls[1][5]
        self.assertEqual(poll["grant_type"], auth.DEVICE_GRANT)
        self.assertTrue(tm.store.load().refresh_token == "r")

    def test_missing_scope_rejected(self):
        self.fake.add(200, {"device_code": "dc", "user_code": "X", "verification_uri": "u", "expires_in": 100, "interval": 1})
        self.fake.add(200, {"access_token": "a", "refresh_token": "r", "expires_in": 100, "scope": []})
        self.fake.add(200, {"login": "me", "user_id": "1", "scopes": []})
        tm = TokenManager("cid", TokenStore(Path(tempfile.mkdtemp()) / "t.json"))
        with self.assertRaises(AuthError):
            tm.interactive_login(print_fn=lambda *_: None, sleep=lambda *_: None)

    def test_denied(self):
        self.fake.add(400, {"message": "access_denied"})
        with self.assertRaises(AuthError):
            auth.poll_device_flow("cid", "dc", 1, 100, sleep=lambda *_: None)

    def test_refresh_includes_secret_only_when_set(self):
        self.fake.add(200, {"access_token": "a", "refresh_token": "r", "expires_in": 100, "scope": "clips:edit"})
        auth.refresh("cid", "old", client_secret="")
        self.assertNotIn("client_secret", self.fake.calls[0][5])
        self.fake.add(200, {"access_token": "a", "refresh_token": "r", "expires_in": 100, "scope": "clips:edit"})
        t = auth.refresh("cid", "old", client_secret="sec")
        self.assertEqual(self.fake.calls[1][5]["client_secret"], "sec")
        self.assertEqual(t.scopes, ["clips:edit"])


class HttpTests(unittest.TestCase):
    def test_list_params_repeat_keys(self):
        captured = {}

        class FakeResp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            return FakeResp()

        with mock.patch.object(http.urllib.request, "urlopen", fake_urlopen):
            r = http.request("GET", "https://x/y", params={"id": ["a", "b"], "n": 1, "skip": None})
        self.assertEqual(captured["url"], "https://x/y?id=a&id=b&n=1")
        self.assertTrue(r.ok)
        self.assertEqual(r.body, {"ok": True})


if __name__ == "__main__":
    unittest.main()

"""Relay server: input validation at the trust boundary, register/update/delete flow. No network."""
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent.parent / "app"), str(Path(__file__).parent.parent / "server")]
import chat  # noqa: E402
import server  # noqa: E402

HOOK = "https://discord.com/api/webhooks/1557050913569378406/" + "a" * 68


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Server(unittest.TestCase):
    def setUp(self):
        server.DATA = Path(tempfile.mkdtemp())
        server.regs.clear()
        self.posts, self.started = [], []
        server.check_webhook = lambda url: "LiveChat XR"
        chat.post_discord = lambda url, text: self.posts.append((url, text))
        server.start = lambda token, delay=0: self.started.append(token)
        server.stop = lambda token: None
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.open = urllib.request.build_opener(NoRedirect).open

    def tearDown(self):
        self.srv.shutdown()

    def post(self, path, **form):
        try:
            r = self.open(self.base + path, urllib.parse.urlencode(form).encode())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()
        return r.status, r.headers, r.read().decode()

    def test_rejects_non_discord_urls_and_bad_channels(self):
        for hook in ("http://127.0.0.1:8100/api/webhooks/1/x", "https://discord.com.evil.io/api/webhooks/1557050913569378406/" + "a" * 68,
                     "https://discord.com/api/webhooks/123/abc", HOOK + "/../../x"):
            self.assertEqual(self.post("/register", platform="tiktok", channel="me", webhook=hook)[0], 400, hook)
        self.assertEqual(self.post("/register", platform="tiktok", channel="<script>", webhook=HOOK)[0], 400)
        self.assertEqual(self.post("/register", platform="youtube", channel="me", webhook=HOOK)[0], 400)
        self.assertEqual(self.post("/register", name="", email="x@y.co", platform="tiktok", channel="me", webhook=HOOK)[0], 400)
        self.assertEqual(self.post("/register", name="A", email="nope", platform="tiktok", channel="me", webhook=HOOK)[0], 400)
        self.assertEqual(server.regs, {})

    def test_admin_page(self):
        self.post("/register", name="Ann <b>", email="ann@example.com", platform="tiktok", channel="ann1", webhook=HOOK)
        server.ADMIN_KEY = "k" * 32
        try:
            body = urllib.request.urlopen(f"{self.base}/admin?key={'k' * 32}").read().decode()
            self.assertIn("ann@example.com", body)
            self.assertIn("Ann &lt;b&gt;", body)
            self.assertNotIn(HOOK, body)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{self.base}/admin?key=wrong")
        finally:
            server.ADMIN_KEY = ""

    def test_register_update_delete(self):
        code, headers, _ = self.post("/register", name="Bren", email="b@example.com", platform="tiktok", channel="crosseyedsensei", webhook=HOOK)
        self.assertEqual(code, 303)
        token = headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(server.regs[token]["channel"], "@crosseyedsensei")
        self.assertIn(f"/m/{token}", self.posts[-1][1])  # manage link lands in their Discord channel
        self.assertTrue((server.DATA / "registrations.json").exists())
        page = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertNotIn(HOOK, page)  # never echo the webhook URL
        # same webhook again = update in place, same token
        code, headers, _ = self.post("/register", name="Bren", email="b@example.com", platform="twitch", channel="#SomeOne", webhook=HOOK)
        self.assertEqual(headers["Location"], f"/m/{token}")
        self.assertEqual((len(server.regs), server.regs[token]["channel"]), (1, "someone"))
        self.assertEqual(self.post(f"/m/{token}/delete")[0], 200)
        self.assertEqual(server.regs, {})
        self.assertEqual(self.post(f"/m/{token}/test")[0], 404)

    def test_cap(self):
        server.MAX_REGS = 1
        try:
            self.assertEqual(self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1", webhook=HOOK)[0], 303)
            self.assertEqual(self.post("/register", name="B", email="b@example.com", platform="tiktok", channel="a2", webhook=HOOK[:-1] + "b")[0], 503)
        finally:
            server.MAX_REGS = 50


if __name__ == "__main__":
    unittest.main()

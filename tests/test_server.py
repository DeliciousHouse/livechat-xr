"""Relay server: input validation at the trust boundary, register/update/delete flow. No network."""
import hashlib
import hmac
import json
import os
import time
import sys
import tempfile
import threading
import unittest
from unittest import mock
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


class ChannelCheck(unittest.TestCase):
    def profile(self, detail):
        return ('<script nonce="test" id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
                + json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": detail}}) + '</script>').encode()

    def check(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch("urllib.request.urlopen", return_value=response) as request:
            result = server.check_channel("tiktok", "@ann1")
        self.assertEqual(request.call_args.args[0].full_url, "https://www.tiktok.com/@ann1")
        self.assertEqual(request.call_args.kwargs["timeout"], 10)
        return result

    def test_existing_offline_profile_is_accepted(self):
        self.assertIs(self.check(self.profile({"statusCode": 0, "userInfo": {"user": {"uniqueId": "Ann1"}}})), True)

    def test_missing_or_unavailable_profile(self):
        for code in (10202, 10221):
            self.assertIs(self.check(self.profile({"statusCode": code})), False)

    def test_unknown_shell_private_or_mismatched_response_is_inconclusive(self):
        for body in (b"captcha", self.profile({"statusCode": 10222}), self.profile({}),
                     self.profile({"statusCode": 0, "userInfo": {"user": {"uniqueId": "someone_else"}}})):
            self.assertIsNone(self.check(body))

    def test_network_and_bad_json_propagate_for_fail_open_registration(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                server.check_channel("tiktok", "@ann1")
        with self.assertRaises(json.JSONDecodeError):
            self.check(b'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{bad}</script>')

    def test_twitch_no_anonymous_check_and_url_host_allowlist(self):
        with mock.patch("urllib.request.urlopen") as request:
            self.assertTrue(server.check_channel("twitch", "ann1"))
            request.assert_not_called()
        for raw in ("https://tiktok.com.evil.test/@ann1", "https://example.com/@ann1", "https://[broken"):
            normal = server.normalise_channel("tiktok", raw)
            self.assertIsNone(server.CHANNEL["tiktok"].match(normal))


class Server(unittest.TestCase):
    def setUp(self):
        server.DATA = Path(tempfile.mkdtemp())
        server.regs.clear()
        server.status.clear()
        self.check_channel = mock.Mock(return_value=True)
        patcher = mock.patch.object(server, "check_channel", self.check_channel)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.posts, self.started = [], []
        server.check_webhook = lambda url: "LiveChat XR"
        self.addCleanup(setattr, chat, "post_discord", chat.post_discord)  # other test modules need the real one
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
        self.assertEqual(self.post("/register", platform="kick", channel="me", webhook=HOOK)[0], 400)
        self.assertEqual(self.post("/register", name="A", email="a@b.co", youtube="me", webhook=HOOK)[0], 400)  # 3+ chars
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
            # activity + failures + log lines show up
            token = next(iter(server.regs))
            server.stats[token] = {"posts": 7, "fails": 2, "last_post": time.time() - 30, "last_error": "10-07 02:00 Discord post failed"}
            server.logbuf.append((time.time(), "WARNING", "livechatxr", "discord post failed: HTTP Error 429 <x>"))
            body = urllib.request.urlopen(f"{self.base}/admin?key={'k' * 32}").read().decode()
            self.assertIn("Discord post failed", body)
            self.assertIn("HTTP Error 429 &lt;x&gt;", body)
            self.assertRegex(body, r"3[01]s ago")
            # backup freshness: missing stamp = red warning, fresh stamp = fine
            self.assertIn("Backup on server: never ⚠", body)
            (server.DATA / "last_backup").write_text(str(time.time() - 3600))
            (server.DATA / "last_offsite").write_text(str(time.time() - 40 * 3600))
            body = urllib.request.urlopen(f"{self.base}/admin?key={'k' * 32}").read().decode()
            self.assertIn("Backup on server: 60m ago.", body.replace("</span>", "."))
            self.assertIn("copy on PC: 40h ago ⚠", body)
            server.stats.clear()
            server.logbuf.clear()
        finally:
            server.ADMIN_KEY = ""

    def test_register_update_delete(self):
        code, headers, _ = self.post("/register", name="Bren", email="b@example.com", platform="tiktok", channel="crosseyedsensei", webhook=HOOK)
        self.assertEqual(code, 303)
        token = headers["Location"].split("?")[0].rsplit("/", 1)[1]
        self.assertEqual(server.regs[token]["channel"], "@crosseyedsensei")
        self.assertIn(f"/m/{token}", self.posts[-1][1])  # manage link lands in their Discord channel
        self.assertTrue((server.DATA / "registrations.json").exists())
        page = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertNotIn(HOOK, page)  # never echo the webhook URL
        # same webhook again = update in place, same token
        code, headers, _ = self.post("/register", name="Bren", email="b@example.com", platform="twitch", channel="#SomeOne", webhook=HOOK)
        self.assertEqual(headers["Location"], f"/m/{token}?new=email")
        self.assertEqual((len(server.regs), server.regs[token]["channel"]), (1, "someone"))
        self.assertEqual(self.post(f"/m/{token}/delete")[0], 200)
        self.assertEqual(server.regs, {})
        self.assertEqual(self.post(f"/m/{token}/test")[0], 404)

    def test_youtube_registration_from_live_url(self):
        code, headers, _ = self.post("/register", name="Ann", email="ann@example.com", webhook=HOOK,
                                     youtube="https://www.youtube.com/@Some_One/live")
        self.assertEqual(code, 303)
        token = headers["Location"].split("?")[0].rsplit("/", 1)[1]
        self.assertEqual(server.regs[token]["channels"], {"youtube": "@Some_One"})
        self.check_channel.assert_any_call("youtube", "@Some_One")

    def test_both_platforms_in_one_registration(self):
        code, _, body = self.post("/register", name="Ann", email="ann@example.com", webhook=HOOK)
        self.assertEqual((code, server.regs), (400, {}))
        self.assertIn("TikTok handle, your Twitch channel, your YouTube handle, or any mix", body)
        code, headers, _ = self.post("/register", name="Ann", email="ann@example.com", webhook=HOOK,
                                     tiktok="https://www.tiktok.com/@Ann.1", twitch=" @SomeOne ")
        self.assertEqual(code, 303)
        token = next(iter(server.regs))
        r = server.regs[token]
        self.assertEqual(r["channels"], {"tiktok": "@Ann.1", "twitch": "someone"})
        self.assertEqual((r["platform"], r["channel"]), ("tiktok", "@Ann.1"))  # primary pair for older readers
        self.assertEqual(self.check_channel.call_args_list, [mock.call("tiktok", "@Ann.1"), mock.call("twitch", "someone")])
        self.assertIn("TikTok @Ann.1 · Twitch someone", self.posts[-1][1])
        manage = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
        self.assertIn("<b>TikTok:</b> @Ann.1<br><b>Twitch:</b> someone", manage)
        self.assertIn("TikTok @Ann.1 · Twitch someone", server.admin_body())
        # a Twitch-only error is reported for Twitch, and both values stay in the form
        self.check_channel.side_effect = lambda p, c: p != "twitch"
        code, _, body = self.post("/register", name="Ann", email="ann@example.com", webhook=HOOK, tiktok="ann1", twitch="gone")
        self.assertEqual(code, 400)
        self.assertIn("find that Twitch account", body)
        self.assertIn('name="tiktok" value="ann1"', body)
        self.assertIn('name="twitch" value="gone"', body)
        # old records without "channels" still resolve
        self.assertEqual(server.channels({"platform": "twitch", "channel": "old"}), {"twitch": "old"})

    def test_missing_channel_keeps_escaped_form_without_saving(self):
        self.check_channel.return_value = False
        code, _, body = self.post("/register", name='Ann "<b>', email="ann@example.com",
                                  platform="twitch", channel="missing", webhook=HOOK)
        self.assertEqual(code, 400)
        self.assertIn("We can&#x27;t find that Twitch account", body)
        self.assertIn('value="Ann &quot;&lt;b&gt;"', body)
        self.assertIn('value="ann@example.com"', body)
        self.assertIn('name="twitch" value="missing"', body)  # the old single-platform form lands in the right field
        self.assertIn(f'value="{HOOK}"', body)
        self.assertEqual((server.regs, self.posts, self.started), ({}, [], []))

    def test_tiktok_missing_error_before_discord_oauth(self):
        with mock.patch.object(server, "DISCORD_ID", "123"):
            self.check_channel.return_value = False
            code, _, body = self.post("/register", name="Ann", email="ann@example.com",
                                      platform="tiktok", channel="missing", via="discord")
        self.assertEqual(code, 400)
        self.assertIn("We can&#x27;t find that TikTok account. Check the spelling, without the @", body)
        self.assertEqual(server.regs, {})

    def test_channel_check_outage_accepts_with_note(self):
        self.check_channel.side_effect = TimeoutError
        code, headers, _ = self.post("/register", name="Ann", email="ann@example.com",
                                     platform="tiktok", channel="ann1", webhook=HOOK)
        self.assertEqual(code, 303)
        body = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
        self.assertIn("couldn&#x27;t verify", body)
        self.assertEqual(server.free_used(), 1)

    def test_inconclusive_check_accepts_with_note_then_verified_update_clears_it(self):
        self.check_channel.return_value = None
        _, headers, _ = self.post("/register", name="Ann", email="ann@example.com",
                                  platform="tiktok", channel="ann1", webhook=HOOK)
        body = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
        self.assertIn("couldn&#x27;t verify", body)
        self.check_channel.return_value = True
        self.post("/register", name="Ann", email="ann@example.com", platform="tiktok", channel="ann1", webhook=HOOK)
        self.assertEqual(next(iter(server.regs.values()))["channel_note"], "")
        self.assertEqual(server.free_used(), 1)

    def test_normalised_channels_reach_check_and_registration(self):
        for platform, raw, expected in (("tiktok", " @Ann.1 ", "@Ann.1"),
                                         ("tiktok", "https://www.tiktok.com/@Ann.1?lang=en", "@Ann.1"),
                                         ("twitch", " @SomeOne ", "someone"),
                                         ("twitch", "https://www.twitch.tv/SomeOne/", "someone")):
            code, _, _ = self.post("/register", name="Ann", email="ann@example.com",
                                   platform=platform, channel=raw, webhook=HOOK)
            self.assertEqual(code, 303, raw)
            self.check_channel.assert_called_with(platform, expected)
            self.assertEqual(next(iter(server.regs.values()))["channel"], expected)

    def test_problem_visible_on_manage_and_admin(self):
        _, headers, _ = self.post("/register", name="Ann", email="ann@example.com",
                                  platform="tiktok", channel="ann1", webhook=HOOK)
        token = next(iter(server.regs))
        server.status[token] = "TikTok: can't find @ann1. Check the spelling or permission to go LIVE; retrying in 30 minutes."
        body = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
        for page in (body, server.admin_body()):
            self.assertIn("Check the spelling or permission to go LIVE", page)
            self.assertIn("retrying in 30 minutes", page)
        self.assertIn("This updates your existing registration", body)

    def webhook_event(self, event, secret="whsec_test", ts=None):
        body = json.dumps(event).encode()
        ts = str(ts or int(time.time()))
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.base + "/stripe-webhook", body, {"Stripe-Signature": f"t={ts},v1={sig}"})
        try:
            return self.open(req).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_free_slots_then_paid(self):
        server.FREE_SLOTS, server.STRIPE_SECRET = 1, "whsec_test"
        server.PAY_MONTHLY = "https://buy.stripe.com/m"
        try:
            hook2 = HOOK[:-1] + "b"
            self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1", webhook=HOOK)
            code, headers, _ = self.post("/register", name="B", email="b@example.com", platform="tiktok", channel="b1", webhook=hook2)
            token = headers["Location"].split("?")[0].rsplit("/", 1)[1]
            self.assertEqual(server.regs[token]["plan"], "pending")
            self.assertEqual(len(self.started), 1)  # the pending one is not running
            page = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
            self.assertIn(f"https://buy.stripe.com/m?client_reference_id={token}", page)
            paid = {"type": "checkout.session.completed",
                    "data": {"object": {"client_reference_id": token, "payment_status": "paid", "subscription": "sub_1"}}}
            self.assertEqual(self.webhook_event(paid, secret="wrong"), 400)  # forged
            self.assertEqual(self.webhook_event(paid, ts=int(time.time()) - 3600), 400)  # replayed
            self.assertEqual(server.regs[token]["plan"], "pending")
            self.assertEqual(self.webhook_event(paid), 200)
            self.assertEqual((server.regs[token]["plan"], self.started[-1]), ("paid", token))
            # a free user can't be "upgraded" by someone paying with their token, and stays free
            free_token = next(t for t, r in server.regs.items() if r["plan"] == "free")
            paid["data"]["object"]["client_reference_id"] = free_token
            self.webhook_event(paid)
            self.assertEqual(server.regs[free_token]["plan"], "free")
            gone = {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}}
            self.assertEqual(self.webhook_event(gone), 200)
            self.assertEqual(server.regs[token]["plan"], "pending")
        finally:
            server.FREE_SLOTS, server.STRIPE_SECRET, server.PAY_MONTHLY = 5, "", ""

    def test_discord_one_click(self):
        server.DISCORD_ID, server.PUBLIC_URL = "123", "https://relay.example"
        server.discord_exchange = lambda code: HOOK if code == "good" else (_ for _ in ()).throw(ValueError("bad code"))
        try:
            self.assertIn(b"Connect Discord", urllib.request.urlopen(self.base + "/").read())
            code, headers, _ = self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1", via="discord")
            loc = urllib.parse.urlparse(headers["Location"])
            q = dict(urllib.parse.parse_qsl(loc.query))
            self.assertEqual((code, loc.netloc, q["scope"], q["redirect_uri"]),
                             (303, "discord.com", "webhook.incoming", "https://relay.example/discord/callback"))
            self.assertEqual(server.regs, {})  # nothing saved until Discord comes back
            bad = self.open  # forged/unknown state is refused
            with self.assertRaises(urllib.error.HTTPError):
                bad(f"{self.base}/discord/callback?state=nope&code=good")
            r = None
            try:
                bad(f"{self.base}/discord/callback?state={q['state']}&code=good")
            except urllib.error.HTTPError as e:
                r = e
            self.assertEqual(r.code, 303)  # redirect to the manage page
            (token, reg), = server.regs.items()
            self.assertEqual((reg["webhook"], reg["channel"], reg["plan"]), (HOOK, "@a1", "free"))
            with self.assertRaises(urllib.error.HTTPError):  # a state works once
                bad(f"{self.base}/discord/callback?state={q['state']}&code=good")
        finally:
            server.DISCORD_ID, server.PUBLIC_URL = "", ""

    def test_log_and_stats_survive_restart(self):
        (server.DATA / "relay.log").write_text("1791370000.5\tWARNING\tlivechatxr\tdiscord post failed: 429\nnot a log line\n",
                                               encoding="utf-8")
        server.logbuf.clear()
        server.load_log_tail()
        self.assertEqual(list(server.logbuf), [(1791370000.5, "WARNING", "livechatxr", "discord post failed: 429")])
        server.write_json("stats.json", {"tok": {"posts": 3}})
        self.assertEqual(json.loads((server.DATA / "stats.json").read_text()), {"tok": {"posts": 3}})
        server.logbuf.clear()

    def test_log_drops_query_strings(self):
        server.ADMIN_KEY = "k" * 32
        try:
            with self.assertLogs("relay", "INFO") as cm:
                urllib.request.urlopen(f"{self.base}/admin?key={'k' * 32}").read()
            self.assertIn("GET /admin", cm.output[-1])
            self.assertFalse(any("k" * 32 in line for line in cm.output), "admin key must not be logged")
        finally:
            server.ADMIN_KEY = ""

    def test_google_sign_in(self):
        server.GOOGLE_ID = "cid.apps.googleusercontent.com"
        server.google_identity = lambda cred: ("Gina G", "gina@example.com") if cred == "good" else (_ for _ in ()).throw(ValueError)
        try:
            self.assertIn(b'data-client_id="cid.apps.googleusercontent.com"', urllib.request.urlopen(self.base + "/").read())
            # the verified Google identity wins over whatever the form says
            code, headers, _ = self.post("/register", google="good", name="x", email="spoof@example.com",
                                         platform="tiktok", channel="g1", webhook=HOOK)
            self.assertEqual((code, headers["Location"].split("?")[1]), (303, "new=google"))
            (reg,) = server.regs.values()
            self.assertEqual((reg["name"], reg["email"], reg["signin"]), ("Gina G", "gina@example.com", "google"))
            self.assertEqual(self.post("/register", google="forged", name="A", email="a@example.com",
                                       platform="tiktok", channel="g1", webhook=HOOK)[0], 400)
        finally:
            server.GOOGLE_ID = ""

    def test_analytics_never_sees_manage_token(self):
        server.GA_ID = "G-TEST123"
        try:
            self.assertIn(b"gtag/js?id=G-TEST123", urllib.request.urlopen(self.base + "/").read())
            _, headers, _ = self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1", webhook=HOOK)
            token = headers["Location"].split("?")[0].rsplit("/", 1)[1]
            body = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
            self.assertIn("location.origin+'/m/'", body)
            self.assertIn("'sign_up',{method:'email'}", body)
            self.assertNotIn(token, body.split("<body>")[0], "token must not reach the analytics config")
            server.ADMIN_KEY = "k" * 32
            self.assertNotIn(b"gtag", urllib.request.urlopen(f"{self.base}/admin?key={'k' * 32}").read())
        finally:
            server.GA_ID = server.ADMIN_KEY = ""

    def test_privacy_page(self):
        body = urllib.request.urlopen(self.base + "/privacy").read().decode()
        self.assertIn("Stop and delete", body)
        self.assertIn('href="/privacy"', urllib.request.urlopen(self.base + "/").read().decode())

    def test_home_assets(self):
        home = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn('import { prepare, layout } from "/pretext.js"', home)
        self.assertIn('name="tiktok" value=""', home)
        self.assertIn('name="twitch" value=""', home)
        r = urllib.request.urlopen(self.base + "/pretext.js")
        self.assertTrue(r.headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"as prepare", r.read())
        self.assertNotIn("pretext", urllib.request.urlopen(self.base + "/privacy").read().decode())  # only the home page loads it

    def test_cap(self):
        server.MAX_REGS = 1
        try:
            self.assertEqual(self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1", webhook=HOOK)[0], 303)
            self.assertEqual(self.post("/register", name="B", email="b@example.com", platform="tiktok", channel="a2", webhook=HOOK[:-1] + "b")[0], 503)
        finally:
            server.MAX_REGS = 50


if __name__ == "__main__":
    unittest.main()

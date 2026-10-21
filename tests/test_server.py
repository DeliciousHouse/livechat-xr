"""Relay server: input validation at the trust boundary, register/update/delete flow. No network."""
import asyncio
import hashlib
import hmac
import json
import os
import socket
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


    def test_youtube_links_resolve_to_the_owning_channel_handle(self):
        # Video/live links must not become unrelated @watch or @channel handles.
        cid = "UC" + "a" * 22
        pages = {"https://www.youtube.com/watch?v=abcDEF12345": '"ownerProfileUrl":"http://www.youtube.com/@StreamerOne"',
                 "https://www.youtube.com/watch?v=noOwner0001": f'"externalChannelId":"{cid}"',
                 "https://www.youtube.com/c/OldName": f'"externalId":"{cid}"',
                 "https://www.youtube.com/user/OldName": f'"externalId":"{cid}"'}
        resolve = lambda platform, handle, identity: {"id": identity["id"], "handle": "@StreamerOne"} if identity["id"] == cid else None
        with mock.patch.object(chat, "_yt_get", side_effect=lambda url: pages.get(url, "")), \
                mock.patch.object(chat, "resolve_identity", side_effect=resolve):
            for raw in ("https://www.youtube.com/watch?v=abcDEF12345&t=3", "https://youtu.be/abcDEF12345?si=x",
                        "youtu.be/abcDEF12345", "youtube.com/live/abcDEF12345",
                        "https://music.youtube.com/watch?v=abcDEF12345", "music.youtube.com/watch?v=abcDEF12345",
                        "https://www.youtube.com/shorts/abcDEF12345", "https://www.youtube.com/watch?v=noOwner0001",
                        f"https://www.youtube.com/channel/{cid}", "https://www.youtube.com/c/OldName",
                        "https://www.youtube.com/user/OldName", "youtube.com/@StreamerOne/live",
                        "https://m.youtube.com/@StreamerOne", "@StreamerOne"):
                with self.subTest(raw=raw):
                    self.assertEqual(server.normalise_channel("youtube", raw), "@StreamerOne")
            for raw in ("https://www.youtube.com/watch?v=", "https://www.youtube.com/watch?v=unknownVid1",
                        "https://www.youtube.com/channel/notAnId", "https://youtube.com.evil.test/watch?v=abcDEF12345",
                        "https://youtu.be.evil.test/abcDEF12345", "https://music.youtube.com.evil.test/watch?v=abcDEF12345",
                        "https://youtube.com@evil.test/watch?v=abcDEF12345"):
                with self.subTest(raw=raw):
                    self.assertIsNone(server.CHANNEL["youtube"].fullmatch(server.normalise_channel("youtube", raw)))
        with mock.patch.object(chat, "_yt_get", side_effect=TimeoutError):
            self.assertIsNone(server.CHANNEL["youtube"].fullmatch(
                server.normalise_channel("youtube", "https://youtu.be/abcDEF12345")))
        self.assertEqual(server.normalise_channel("tiktok", "tiktok.com/@ann1"), "@ann1")
        self.assertEqual(server.normalise_channel("twitch", "www.twitch.tv/Ann1"), "ann1")


class Server(unittest.TestCase):
    def setUp(self):
        server.DATA = Path(tempfile.mkdtemp())
        server.regs.clear()
        server.founders.clear()
        server.status.clear()
        server.stats.clear()
        server.reports.clear()
        resolver = mock.patch.object(chat, "resolve_identity", return_value=None)
        resolver.start()
        self.addCleanup(resolver.stop)
        self.check_channel = mock.Mock(return_value=True)
        patcher = mock.patch.object(server, "check_channel", self.check_channel)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.posts, self.started = [], []
        server.check_webhook = lambda url: {"name": "LiveChat XR"}
        self.addCleanup(setattr, chat, "post_discord", chat.post_discord)  # other test modules need the real one
        chat.post_discord = lambda url, text, **kwargs: self.posts.append((url, text))
        server.start = lambda token, delay=0: self.started.append(token)
        server.stop = lambda token: None
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.open = urllib.request.build_opener(NoRedirect).open

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

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
        self.assertIn("<b>TikTok:</b> @Ann.1", manage)
        self.assertIn("<b>Twitch:</b> someone", manage)
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
        self.assertIn("Check the spelling below", body)
        self.assertIn("Edit settings", body)
        self.assertIn("Check the spelling or permission to go LIVE", server.admin_body())

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
            self.assertEqual(server.regs[token]["plan"], "free")
            self.assertEqual(len(self.started), 2)
            page = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
            self.assertIn(f"https://buy.stripe.com/m?client_reference_id={token}", page)
            paid = {"type": "checkout.session.completed",
                    "data": {"object": {"client_reference_id": token, "payment_status": "paid", "subscription": "sub_1"}}}
            self.assertEqual(self.webhook_event(paid, secret="wrong"), 400)  # forged
            self.assertEqual(self.webhook_event(paid, ts=int(time.time()) - 3600), 400)  # replayed
            self.assertEqual(server.regs[token]["plan"], "free")
            self.assertEqual(self.webhook_event(paid), 200)
            self.assertEqual((server.regs[token]["plan"], self.started[-1]), ("paid", token))
            # Founders never lose their lifetime entitlement.
            free_token = next(t for t, r in server.regs.items() if r["plan"] == "founder")
            paid["data"]["object"]["client_reference_id"] = free_token
            self.webhook_event(paid)
            self.assertEqual(server.regs[free_token]["plan"], "founder")
            gone = {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}}
            self.assertEqual(self.webhook_event(gone), 200)
            self.assertEqual(server.regs[token]["plan"], "free")
        finally:
            server.FREE_SLOTS, server.STRIPE_SECRET, server.PAY_MONTHLY = 5, "", ""

    def test_donation_checkout_ignored_and_cancellation_keeps_all_channels(self):
        self.post("/register", name="Ann", email="ann@example.com", tiktok="ann1", twitch="someone", webhook=HOOK)
        token = next(iter(server.regs))
        server.regs[token].update(plan="paid", subscription="sub_test")
        with mock.patch.object(server, "DONATE_URL", "https://buy.stripe.com/donate"), \
                mock.patch.object(server, "STRIPE_SECRET", "whsec_test"):
            before = dict(server.regs[token])
            self.assertEqual(self.webhook_event({"type": "checkout.session.completed", "data": {"object": {
                "payment_status": "paid"}}}), 200)
            self.assertEqual(server.regs[token], before)
            self.assertEqual(self.webhook_event({"type": "customer.subscription.deleted", "data": {"object": {
                "id": "sub_test"}}}), 200)
            self.assertEqual(server.plan(server.regs[token]), "free")
            self.assertEqual(len(self.live_channels(token)), 2)
            self.assertIn("All your saved channels stay free", self.posts[-1][1])
            self.assertNotIn("waiting for upgrade", self.posts[-1][1])

    def live_channels(self, token):
        relay = mock.AsyncMock(side_effect=asyncio.CancelledError)
        with mock.patch.object(chat, "relay", relay):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(server.run(token))
        return relay.call_args.kwargs["channels"]

    def test_fifth_founder_sixth_free_upgrade_and_downgrade(self):
        for i in range(6):
            code, headers, _ = self.post("/register", name="QA", email="qa@example.com",
                                         tiktok="ann1", twitch="someone", youtube="third",
                                         webhook=HOOK[:-1] + str(i))
            self.assertEqual(code, 303)
        tokens = list(server.regs)
        founder, token = tokens[4:]
        self.assertEqual(server.plan(server.regs[founder]), "founder")
        self.assertEqual(len(self.live_channels(founder)), 3)
        self.assertEqual(server.plan(server.regs[token]), "free")
        self.assertEqual(self.live_channels(token), {"tiktok": "@ann1"})
        self.assertEqual(self.started, tokens)
        with mock.patch.object(server, "STRIPE_SECRET", "whsec_test"), \
                mock.patch.object(server, "PAY_MONTHLY", "https://buy.stripe.com/m"), \
                mock.patch.object(server, "PAY_YEARLY", "https://buy.stripe.com/y"):
            body = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
            self.assertIn("Twitch:</b> someone", body)
            self.assertIn("YouTube:</b> @third", body)
            self.assertEqual(body.count("Waiting for upgrade"), 2)
            for link in ("m", "y"):
                self.assertIn(f"https://buy.stripe.com/{link}?client_reference_id={token}", body)
            paid = {"type": "checkout.session.completed", "data": {"object": {
                "client_reference_id": token, "payment_status": "paid", "subscription": "sub_1"}}}
            self.assertEqual(self.webhook_event(paid), 200)
            self.assertEqual(server.plan(server.regs[token]), "paid")
            self.assertEqual(len(self.live_channels(token)), 3)
            self.assertEqual(self.started[-1], token)
            gone = {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}}
            self.assertEqual(self.webhook_event(gone), 200)
            self.assertEqual(server.plan(server.regs[token]), "free")
            self.assertEqual(self.live_channels(token), {"tiktok": "@ann1"})
            self.assertEqual(len(server.channels(server.regs[token])), 3)
            self.assertEqual(self.started[-1], token)
            self.assertIn("5/5 founders, 1 free, 0 paid, 1 waiting upgrade", server.admin_body())
        self.post(f"/m/{founder}/delete")
        server.regs.clear()
        server.founders.clear()
        server.load_registrations()
        self.assertEqual(server.plan(server.regs[token]), "free")
        self.post("/register", name="QA", email="qa@example.com", twitch="single", webhook=HOOK[:-1] + "6")
        single = self.started[-1]
        self.assertEqual(server.plan(server.regs[single]), "free")
        self.assertEqual(self.live_channels(single), {"twitch": "single"})

    def test_migration_preserves_four_founders_and_backup(self):
        original = {str(i): dict(platform="tiktok", channel="@ann1", created=4-i, webhook=HOOK,
                                channels={"tiktok": "@ann1", "twitch": "someone"},
                                **({"plan": "free"} if i % 2 else {})) for i in range(4)}
        original["pending"] = dict(platform="twitch", channel="waiting", created=0, plan="pending")
        original["paid"] = dict(platform="twitch", channel="payer", created=0, plan="paid")
        server.write_json("registrations.json", original)
        before = (server.DATA / "registrations.json").read_bytes()
        server.load_registrations()
        self.assertEqual((server.DATA / "registrations.pre-founder.json").read_bytes(), before)
        for i in range(4):
            self.assertEqual(server.plan(server.regs[str(i)]), "founder")
            self.assertEqual(len(self.live_channels(str(i))), 2)
        self.assertEqual(server.plan(server.regs["pending"]), "free")
        self.assertEqual(server.plan(server.regs["paid"]), "paid")
        migrated = (server.DATA / "registrations.json").read_bytes()
        server.regs.clear()
        server.founders.clear()
        server.load_registrations()
        self.assertEqual((server.DATA / "registrations.json").read_bytes(), migrated)
        self.assertEqual((server.DATA / "registrations.pre-founder.json").read_bytes(), before)

    def test_migration_orders_by_created_with_token_tiebreak(self):
        server.write_json("registrations.json", {t: dict(created=i, plan="free")
                                                 for t, i in [("z", 0), ("a", 0), ("b", 1)]})
        with mock.patch.object(server, "FREE_SLOTS", 1):
            server.load_registrations()
        self.assertEqual(server.plan(server.regs["a"]), "founder")
        self.assertEqual(server.plan(server.regs["z"]), "free")

    def test_migration_backup_failure_prevents_writes(self):
        server.write_json("registrations.json", {"old": dict(created=1, plan="free")})
        before = (server.DATA / "registrations.json").read_bytes()
        original_open = Path.open
        def open_backup(path, mode="r", *args, **kwargs):
            if path.name == "registrations.pre-founder.json" and mode == "xb":
                raise PermissionError("backup denied")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", open_backup):
            with self.assertRaises(PermissionError):
                server.load_registrations()
        self.assertEqual((server.DATA / "registrations.json").read_bytes(), before)
        self.assertFalse((server.DATA / "founders.json").exists())

    def test_migration_resumes_after_reservation_write(self):
        server.write_json("registrations.json", {"old": dict(created=1, plan="free")})
        with mock.patch.object(server, "save", side_effect=OSError):
            with self.assertRaises(OSError):
                server.load_registrations()
        server.regs.clear()
        server.founders.clear()
        server.load_registrations()
        self.assertEqual(server.plan(server.regs["old"]), "founder")

    def test_pricing_and_founder_manage_copy(self):
        self.assertIn("First 5 streamers: everything free.", server.home().decode())
        self.assertIn("After that: 1 channel free, multiple channels on a paid plan", server.home().decode())
        with mock.patch.object(server, "FREE_SLOTS", 1):
            self.assertIn("First 1 streamer: everything free.", server.home().decode())
        _, headers, _ = self.post("/register", name="QA", email="qa@example.com", twitch="single", webhook=HOOK)
        body = urllib.request.urlopen(self.base + headers["Location"]).read().decode()
        self.assertIn("Founder: all your channels free forever.", body)
        self.assertNotIn("client_reference_id", body)

    def discord(self, me, path="/register", hook=HOOK, cookie=True, **form):
        """Press the Discord button, then come back from Discord as `me`. Returns (code, headers, body, authorize query)."""
        code, headers, _ = self.post(path, **form)
        self.assertEqual(code, 303)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(headers["Location"]).query))
        jar = headers["Set-Cookie"].split(";")[0] if cookie is True else cookie
        token = {"token_type": "Bearer", "access_token": "fixture"} | ({"webhook": {"url": hook}} if "webhook" in query["scope"] else {})
        with mock.patch.object(server, "discord_oauth", return_value=(token, me)):
            request = urllib.request.Request(f"{self.base}/discord/callback?state={query['state']}&code=good",
                                             headers={"Cookie": jar} if jar else {})
            try:
                with self.open(request) as response:
                    return response.status, response.headers, response.read().decode(), query
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read().decode(), query

    ANN = {"id": "42", "username": "ann", "global_name": "Ann A", "email": "Ann@Example.com", "verified": True}

    def test_discord_sign_up_stores_identity_and_log_in_finds_it(self):
        with mock.patch.multiple(server, DISCORD_ID="123", PUBLIC_URL="https://relay.example"):
            home = urllib.request.urlopen(self.base + "/").read().decode()
            self.assertIn(">Log in or sign up with Discord</button>", home)
            self.assertNotIn('name="email"', home)  # identity comes from Discord
            self.assertNotIn('name="webhook"', home)  # a pasted webhook can't create a registration
            code, headers, body, query = self.discord(self.ANN, twitch="ann1")
            self.assertEqual((query["scope"], query["redirect_uri"]), ("identify email webhook.incoming",
                                                                        "https://relay.example/discord/callback"))
            (token, reg), = server.regs.items()
            self.assertEqual((code, headers["Location"]), (303, f"/m/{token}?new=discord"))
            self.assertEqual((reg["discord_id"], reg["discord_username"], reg["name"], reg["email"], reg["signin"], reg["webhook"]),
                             ("42", "ann", "Ann A", "ann@example.com", "discord", HOOK))
            # Log in from /recover or from the home button with no channels: identify only, no webhook, no new relay.
            for path in ("/recover", "/register"):
                code, headers, _, query = self.discord(self.ANN, path=path)
                self.assertEqual((query["scope"], code, headers["Location"]), ("identify", 303, f"/m/{token}"))
            # The same Discord account signing up the same stream again moves it to the new channel, no duplicate.
            code, headers, _, _ = self.discord(self.ANN, hook=HOOK[:-1] + "b", twitch="ANN1")
            self.assertEqual((code, headers["Location"]), (303, f"/m/{token}?new=discord&moved=1"))
            self.assertEqual((list(server.regs), server.regs[token]["webhook"]), ([token], HOOK[:-1] + "b"))
            # Existing manage-token flows are unchanged; Discord may share no email, so settings save without one.
            self.assertEqual(self.post(f"/m/{token}/save", name="Ann", email="", twitch="ann1")[0], 303)
            self.assertEqual(self.post(f"/m/{token}/test")[0], 200)
            self.assertIn("log in with Discord", urllib.request.urlopen(f"{self.base}/m/{token}").read().decode())
            self.assertEqual(self.post(f"/m/{token}/delete")[0], 200)
            code, _, body, _ = self.discord(self.ANN, path="/recover")
            self.assertEqual((code, server.regs), (200, {}))
            self.assertIn("No relay is linked to this Discord account yet", body)

    def test_discord_relays_per_account_are_capped(self):
        with mock.patch.multiple(server, DISCORD_ID="123", PUBLIC_URL="https://relay.example"):
            for i in range(server.REGS_PER_DISCORD_USER):
                self.assertEqual(self.discord(self.ANN, hook=HOOK[:-1] + str(i), twitch=f"stream{i}")[0], 303)
            code, _, body, _ = self.discord(self.ANN, hook=HOOK[:-1] + "x", twitch="another")
            self.assertEqual((code, len(server.regs)), (409, server.REGS_PER_DISCORD_USER))
            self.assertIn("already has 3 relays", body)
            code, _, body, _ = self.discord(self.ANN, path="/recover")
            self.assertEqual(code, 200)
            self.assertTrue(all(f"/m/{t}" in body for t in server.regs))
            self.assertEqual(self.discord(dict(self.ANN, id="43"), hook=HOOK[:-1] + "y", twitch="another")[0], 303)

    def test_discord_state_is_bound_to_the_starting_browser(self):
        with mock.patch.multiple(server, DISCORD_ID="123", PUBLIC_URL="https://relay.example"):
            for cookie in ("", "lcx_oauth=someone-elses-state"):
                with mock.patch.object(server, "discord_person", side_effect=AssertionError("must not exchange")):
                    code, _, body, query = self.discord(self.ANN, cookie=cookie, twitch="ann1")
                self.assertEqual((code, server.regs), (400, {}))
                self.assertIn("finished in a different browser", body)
                self.assertIn('name="twitch" value="ann1"', body)  # start again here without retyping
                self.assertNotIn(query["state"], server.oauth)  # single use, even on a mismatch
            _, headers, _ = self.post("/register", twitch="ann1")
            self.assertEqual(headers["Set-Cookie"].split(";", 1)[1].strip(),
                             "Max-Age=900; Path=/discord/callback; SameSite=Lax; HttpOnly; Secure")

    def test_discord_person_keeps_only_verified_email(self):
        self.assertEqual(server.discord_person({"id": "1", "username": "u", "email": "u@example.com", "verified": False}),
                         {"id": "1", "username": "u", "name": "u", "email": ""})
        self.assertEqual(server.discord_person({"id": "1", "username": "u", "email": None, "verified": True})["email"], "")

    def test_posted_webhook_cannot_create_a_registration_with_discord_on(self):
        with mock.patch.multiple(server, DISCORD_ID="123", PUBLIC_URL="https://relay.example"):
            code, headers, _ = self.post("/register", name="A", email="a@example.com", twitch="ann1", webhook=HOOK)
            self.assertEqual((code, urllib.parse.urlparse(headers["Location"]).netloc, server.regs), (303, "discord.com", {}))

    def test_discord_one_click_hidden_without_public_url(self):
        # A relative redirect_uri makes Discord answer "Not a well formed URL" (2026-10-14 sign-up failure).
        server.DISCORD_ID, server.PUBLIC_URL = "123", ""
        try:
            self.assertNotIn(b'value="discord"', urllib.request.urlopen(self.base + "/").read())  # no one-click button
            code, headers, body = self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1",
                                            via="discord")
            self.assertNotIn("discord.com/oauth2", headers.get("Location") or "")
            self.assertIn("It should start with https://discord.com/api/webhooks/", body)  # falls back to the paste-a-webhook path
            self.assertEqual(server.regs, {})
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

    def test_home_discord_installation_prerequisite(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertEqual(response.status, 200)
            body = response.read().decode()
        self.assertIn('<p class="sub" data-pretext>TikTok LIVE, Twitch, YouTube or any mix. '
                      'No PC needed. Requires Discord on your Quest.</p>', body)
        self.assertIn('<p data-pretext>Install Discord on your Quest if you have not already, '
                      'open the channel, and set notifications to <b>All Messages</b>.</p>', body)
        self.assertNotIn("nothing to install", body)

    def test_home_assets(self):
        home = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn('import { prepare, layout } from "/pretext.js"', home)
        self.assertIn('name="tiktok" value=""', home)
        self.assertIn('name="twitch" value=""', home)
        r = urllib.request.urlopen(self.base + "/pretext.js")
        self.assertTrue(r.headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"as prepare", r.read())
        self.assertNotIn("pretext", urllib.request.urlopen(self.base + "/privacy").read().decode())  # only the home page loads it

    def test_security_headers_on_every_response_path(self):
        for path in ("/", "/health", "/nope", "/go/" + "a" * 22, "/m/" + "x" * 30):
            try:
                headers = self.open(self.base + path).headers
            except urllib.error.HTTPError as e:
                headers = e.headers
            with self.subTest(path=path):
                self.assertEqual(headers["Server"], "LiveChatXR")  # no Python version banner
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertIn("form-action 'self' https://discord.com", headers["Content-Security-Policy"])
                for name, value in server.SECURITY_HEADERS.items():
                    self.assertEqual(headers.get_all(name), [value])

    def test_malformed_request_gets_400_not_a_handler_crash(self):
        # log_message runs before parse_request sets self.path (bad request line, socket timeout).
        errors = []
        self.srv.handle_error = lambda request, address: errors.append(address)
        with socket.create_connection(("127.0.0.1", self.srv.server_port), timeout=5) as conn:
            conn.sendall(b"\x16\x03\x01 junk request line here\r\n\r\n")
            reply = conn.recv(4096)
        self.assertIn(b"Error code: 400", reply)  # HTTP/0.9-style error body: no status line to parse
        self.assertEqual(errors, [])

    def test_cross_site_form_posts_are_rejected(self):
        form = urllib.parse.urlencode(dict(name="A", email="a@example.com", tiktok="a1", webhook=HOOK)).encode()
        request = urllib.request.Request(self.base + "/register", form, {"Sec-Fetch-Site": "cross-site"})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.open(request)
        self.assertEqual((rejected.exception.code, server.regs), (403, {}))
        request = urllib.request.Request(self.base + "/register", form, {"Sec-Fetch-Site": "same-origin"})
        with self.assertRaises(urllib.error.HTTPError) as accepted:  # NoRedirect surfaces the 303
            self.open(request)
        self.assertEqual(accepted.exception.code, 303)

    def test_register_and_recover_are_rate_limited_per_forwarded_client(self):
        server.RATE_LIMIT, server.hits = (2, 600), {}
        try:
            def attempt(path, client):
                request = urllib.request.Request(self.base + path, b"name=A", {"X-Forwarded-For": "198.51.100.7, " + client})
                try:
                    return self.open(request).status
                except urllib.error.HTTPError as e:
                    return e.code
            self.assertEqual([attempt("/register", "203.0.113.9") for _ in range(2)], [400, 400])
            self.assertEqual(attempt("/recover", "203.0.113.9"), 429)  # spoofed first hop is ignored
            self.assertEqual(attempt("/register", "203.0.113.10"), 400)  # other clients unaffected
            self.assertFalse(server.rate_limited("192.0.2.1", now=0) or server.rate_limited("192.0.2.1", now=1))
            self.assertTrue(server.rate_limited("192.0.2.1", now=2))
            self.assertFalse(server.rate_limited("192.0.2.1", now=700))  # window slides
        finally:
            server.RATE_LIMIT, server.hits = (20, 600), {}

    def test_cap(self):
        server.MAX_REGS = 1
        try:
            self.assertEqual(self.post("/register", name="A", email="a@example.com", platform="tiktok", channel="a1", webhook=HOOK)[0], 303)
            self.assertEqual(self.post("/register", name="B", email="b@example.com", platform="tiktok", channel="a2", webhook=HOOK[:-1] + "b")[0], 503)
        finally:
            server.MAX_REGS = 50


if __name__ == "__main__":
    unittest.main()

"""Private web surfaces: real HTTP/SQLite/loop, isolated OAuth and Discord transports."""
import asyncio
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from nacl.signing import SigningKey

from tests.test_server import NoRedirect, server
from tests.test_network import row
import network


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.net = network.Network(self.tmp.name, server.normalise_channel, {k: "123" for k in network.REQUIRED})
        self.net.store.initialize()
        self.net.loop = self.loop
        self.net.enabled = True
        self.net.missing = []
        self.signing_key = SigningKey.generate()
        self.net.config["DISCORD_PUBLIC_KEY"] = self.signing_key.verify_key.encode().hex()
        self.payloads = []
        self.condition = threading.Condition()
        def transport(method, path, payload):
            with self.condition:
                self.payloads.append(payload or {})
                self.condition.notify_all()
            return 200, [] if method == "GET" else {"id": "fixture-message"}
        self.net._discord = transport
        self.token = "fixture-registration-0001"
        self.other = "fixture-registration-0002"
        self.reg = dict(name="Fixture streamer", email="", webhook="", discord_channel="Private fixture",
                        platform="twitch", channel="streamer", channel_id="1", guild_id="1", plan="founder", created=0)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        for name, value in (("DATA", Path(self.tmp.name)), ("regs", {self.token: self.reg, self.other: dict(self.reg)}),
                            ("discovery", self.net), ("loop", self.loop), ("NETWORK_WEB_PRIVATE", True),
                            ("PUBLIC_URL", self.base), ("DISCORD_ID", "123"), ("DISCORD_SECRET", "fixture"),
                            ("ADMIN_KEY", "fixture"), ("GA_ID", "G-fixture"), ("logbuf", []),
                            ("network_oauth", {}), ("network_csrf", {})):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.http = threading.Thread(target=self.srv.serve_forever)
        self.http.start()
        self.open = urllib.request.build_opener(NoRedirect).open

    def tearDown(self):
        self.srv.shutdown()
        self.http.join()
        self.srv.server_close()
        asyncio.run_coroutine_threadsafe(self.net.stop(), self.loop).result(3)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()

    def request(self, path, form=None, origin=True):
        req = urllib.request.Request(self.base + path,
            None if form is None else urllib.parse.urlencode(form).encode(),
            {"Origin": self.base if origin is True else origin} if origin else {})
        try: response = self.open(req, timeout=4)
        except urllib.error.HTTPError as error: response = error
        with response: return response.status, response.headers, response.read().decode()

    def manage(self, token=None):
        return self.request("/m/" + (token or self.token))

    def post(self, action, token=None, **values):
        token = token or self.token
        body = self.manage(token)[2]
        csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
        return self.request(f"/m/{token}/network", dict(action=action, csrf=csrf, **values))

    def connect(self, owner="7", token=None):
        token = token or self.token
        self.post("consent", token, listed="yes")
        result = self.post("link", token)
        self.assertEqual(result[0], 303)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result[1]["Location"]).query)
        self.assertEqual(query["scope"], ["identify"])
        self.assertEqual(query["redirect_uri"], [self.base + "/network/discord/callback"])
        state = query["state"][0]
        with mock.patch.object(server, "discord_identify", return_value=owner):
            result = self.request("/network/discord/callback?" + urllib.parse.urlencode(dict(state=state, code="fixture")))
        return result, state

    def signed(self, name, owner="7", **fields):
        body = dict(type=2, id=str(time.monotonic_ns()), application_id="123", token="fixture",
            guild_id="123", member={"user": {"id": owner}}, data={"name": name,
            "options": [{"name": k, "value": v} for k, v in fields.items()]})
        raw = json.dumps(body).encode()
        stamp = str(int(time.time()))
        signature = self.signing_key.sign(stamp.encode() + raw).signature.hex()
        req = urllib.request.Request(self.base + "/discord/interactions", raw,
            {"X-Signature-Timestamp": stamp, "X-Signature-Ed25519": signature})
        with self.open(req, timeout=3) as response:
            self.assertEqual(json.load(response)["type"], 5)

    def wait_payload(self, text):
        with self.condition:
            self.assertTrue(self.condition.wait_for(
                lambda: any(text in p.get("content", "") for p in self.payloads), timeout=3))

    def test_private_gate_security_state_and_conflict(self):
        with mock.patch.object(server, "NETWORK_WEB_PRIVATE", False):
            self.assertNotIn("Pop1 Live listing", self.manage()[2])
            self.assertEqual(self.request(f"/m/{self.token}/network", dict(action="consent"))[0], 404)
        self.assertEqual(self.request("/m/not-a-registration-000/network", {})[0], 404)
        body = self.manage()[2]
        csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
        for origin, value in ((False, csrf), ("https://evil.test", csrf), (True, "wrong")):
            self.assertEqual(self.request(f"/m/{self.token}/network", dict(action="consent", csrf=value), origin)[0], 403)
        self.assertEqual(self.request(f"/m/{self.token}/network?action=optout")[0], 404)
        result, state = self.connect()
        self.assertEqual(result[0], 303)
        with mock.patch.object(server, "discord_identify") as exchange:
            self.assertEqual(self.request(f"/network/discord/callback?state={state}&code=fixture")[0], 400)
            exchange.assert_not_called()
        result, _ = self.connect(token=self.other)
        self.assertEqual(result[0], 409)
        self.assertNotIn("network_listing", server.regs[self.other])
        lid = self.reg["network_listing"]
        self.assertEqual(self.net.store.profile(lid)["discord_id"], "7")
        # Existing association cannot be swapped to a different upstream identity.
        result, _ = self.connect(owner="8")
        self.assertEqual(result[0], 409)
        self.assertEqual(self.net.store.profile(lid)["discord_id"], "7")
        self.assertIsNone(self.net.store.owner_listing("8"))
        # State is consumed before network access and bound to a still-consenting registration.
        result = self.post("link")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(result[1]["Location"]).query)["state"][0]
        created, token = server.network_oauth[state]
        server.network_oauth[state] = (created - 901, token)
        with mock.patch.object(server, "discord_identify") as exchange:
            self.assertEqual(self.request(f"/network/discord/callback?state={state}&code=fixture")[0], 400)
            exchange.assert_not_called()
        for path in (f"/m/{self.token}", "/network/discord/callback", "/admin?key=fixture"):
            _, headers, text = self.request(path)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn("googletagmanager", text)

    def test_checklist_profile_proof_review_and_two_am_removal(self):
        self.assertIn("Not requested", self.manage()[2])
        self.connect()
        self.assertIn("Setting up (step 3 of 5)", self.manage()[2])
        lid = self.reg["network_listing"]
        # Posted IDs/verification/review never become authority.
        self.assertEqual(self.post("prove", listing_id="999", discord_id="8", verified="1")[0], 400)
        self.assertEqual(self.net.store.channels(), [])
        for ign, zone, field in (("<script>", "Europe/London", "ign"), ("Good", "Mars/Nowhere", "timezone")):
            code, _, text = self.post("profile", ign=ign, timezone=zone)
            self.assertEqual(code, 400)
            self.assertIn(f'id="{field}-error"', text)
        code, _, text = self.post("profile", ign="Fixture player", timezone="Other", other_timezone="Europe/London")
        self.assertEqual(code, 200)
        self.assertIn("Setting up (step 4 of 5)", text)
        self.assertEqual(self.net.store.profile(lid)["timezone"], "Europe/London")
        code, _, text = self.post("prove")
        self.assertEqual(code, 200)
        self.assertIn("/optin", text)
        self.assertFalse(self.net.store.profile(lid)["alerts_enabled"])
        # Existing challenge engine, not a web token/handle/existence shortcut.
        self.net.resolve_identity = mock.AsyncMock(return_value={"id": "42", "handle": "streamer"})
        self.net.proof_visible = mock.AsyncMock(return_value=True)
        async def minute(_): await asyncio.sleep(.01)
        with mock.patch.object(network, "wait_retry", minute):
            self.signed("optin", platform="twitch", handle="streamer")
            self.wait_payload("Verified ✓")
        self.assertIn("Waiting for review (within 24h)", self.manage()[2])
        self.assertEqual(self.net.store.discovery(), [])
        async def review(owner):
            return await self.net.command(dict(guild_id="123", member={"user": {"id": owner}},
                data={"name": "review", "options": [{"name": "listing", "value": lid}, {"name": "decision", "value": "approve"}]}))
        with self.assertRaises(PermissionError):
            asyncio.run_coroutine_threadsafe(review("7"), self.loop).result(3)
        self.signed("review", owner="123", listing=lid, decision="approve")
        self.wait_payload("you're on")
        self.net.store.upsert_presence("twitch", [row()], time.time())
        self.assertIn("Listed when you're live", self.manage()[2])
        self.assertEqual(len(self.net.store.discovery()), 1)
        two_am = network.datetime(2026, 10, 17, 2, tzinfo=network.PACIFIC).timestamp()
        with mock.patch.object(server, "NETWORK_WEB_PRIVATE", False), mock.patch.object(network.time, "time", return_value=two_am):
            self.assertIn('value="optout"', self.manage()[2])
            self.assertEqual(self.post("optout")[0], 200)
        self.assertEqual(self.net.store.discovery(), [])
        self.assertEqual(self.net.store.channels(), [])
        self.assertIn("Removed", self.manage()[2])
        self.assertFalse(self.reg["network_requested"])

    def test_public_policy_admin_and_accessible_markup(self):
        for path in ("/", "/privacy", "/terms"):
            code, _, text = self.request(path)
            self.assertEqual(code, 200)
            for href in ("/privacy", "/terms"):
                self.assertIn(f'href="{href}"', text)
            self.assertIn("YouTube", text)
        privacy = self.request("/privacy")[2]
        for phrase in ("https://policies.google.com/privacy", "https://www.youtube.com/t/terms",
                       "24 hours", "7 days", "90 days", "historic backups", "census", "/optout"):
            self.assertIn(phrase, privacy)
        self.assertNotIn("We don't post anywhere except", privacy)
        self.assertIn("YouTube Terms of Service", self.request("/terms")[2])
        self.connect()
        body = self.manage()[2]
        section = body.split('<section id="network-listing"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(section.count('class="n"'), 5)
        self.assertEqual(section.count('class="network-next"'), 1)
        for label in ("List me", "Link Discord", "In-game name + timezone", "Prove your channel", "Review"):
            self.assertIn(label, section)
        for marker in ('Intl.DateTimeFormat().resolvedOptions().timeZone', 'min-height:44px',
                       '2px solid var(--faint)', 'var(--focus)', 'for="ign"', 'for="timezone"', 'Other'):
            self.assertIn(marker, body)
        lid = self.reg["network_listing"]
        for _ in range(2): self.net.store.update_profile(lid, would_pay=True)
        admin = self.request("/admin?key=fixture")[2]
        self.assertIn("1 authenticated streamer", admin)
        self.assertIn("Opt-ins: 1", admin)
        self.assertIn("Outside-cohort count unavailable", admin)
        self.assertIn("E6 visitor measurement unavailable", admin)
        self.assertEqual(self.request("/admin")[0], 404)

    def test_callback_withdrawal_during_exchange_and_failed_save(self):
        self.post("consent", listed="yes")
        result = self.post("link")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(result[1]["Location"]).query)["state"][0]
        entered, release = threading.Event(), threading.Event()
        def exchange(code):
            entered.set()
            self.assertTrue(release.wait(3))
            return "7"
        results = []
        with mock.patch.object(server, "discord_identify", exchange):
            callback = threading.Thread(target=lambda: results.append(self.request(
                f"/network/discord/callback?state={state}&code=fixture")))
            callback.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.post("optout")[0], 200)
                self.assertEqual(self.post("consent", listed="yes")[0], 200)
            finally:
                release.set()
                callback.join()
        self.assertEqual(results[0][0], 409)
        self.assertIsNone(self.net.store.owner_listing("7"))
        self.connect()
        before = dict(self.reg)
        with mock.patch.object(server, "save", side_effect=OSError):
            self.assertEqual(self.post("optout")[0], 503)
        self.assertEqual(self.reg, before)
        self.assertTrue(self.net.store.profile(self.reg["network_listing"])["opted_in"])

    def test_removed_or_reassigned_record_never_grants_another_owner(self):
        self.connect()
        lid = self.reg["network_listing"]
        csrf = re.search(r'name="csrf" value="([^"]+)"', self.manage()[2])[1]
        self.net.store.set_listing(lid, discord_id="8")
        self.assertIn("Needs attention", self.manage()[2])
        self.assertEqual(self.request(f"/m/{self.token}/network", dict(action="profile", csrf=csrf, ign="Hijack", timezone="Europe/London"))[0], 409)
        self.assertEqual(self.request(f"/m/{self.token}/network", dict(action="optout", csrf=csrf))[0], 409)
        self.assertEqual(self.net.store.profile(lid)["ign"], "")
        self.assertTrue(self.net.store.profile(lid)["opted_in"])

    def test_removal_pending_remote_failure_and_loop_responsiveness(self):
        self.connect()
        lid = self.reg["network_listing"]
        self.net.live_message = ("123", "fixture-list")
        self.net.naming_messages[("123", "fixture-ping")] = {lid}
        entered, release = threading.Event(), threading.Event()
        def slow_transport(method, path, payload):
            if method == "PATCH":
                entered.set()
                self.assertTrue(release.wait(3))
                raise OSError("fixture transport unavailable")
            return 200, {"id": "fixture"}
        actual_submit = asyncio.run_coroutine_threadsafe
        futures = []
        def submit(coro, loop):
            future = actual_submit(coro, loop)
            futures.append(future)
            proxy = mock.Mock()
            proxy.result.side_effect = lambda timeout: future.result(timeout=.1)
            return proxy
        with mock.patch.object(self.net, "_discord", slow_transport), mock.patch.object(server.asyncio, "run_coroutine_threadsafe", submit):
            try:
                code, _, text = self.post("optout")
                self.assertEqual(code, 202)
                self.assertIn("still pending", text)
                self.assertTrue(entered.wait(2))
                async def ping(): return "loop alive"
                self.assertEqual(actual_submit(ping(), self.loop).result(.5), "loop alive")
                self.assertFalse(self.net.store.profile(lid)["opted_in"])
                self.assertEqual(self.net.store.channels(), [])
            finally: release.set()
            self.assertFalse(futures[0].result(3))
        self.assertEqual(len(self.net.reconcile), 2)

    def test_incident_off_first_stale_rows_and_healthy_line(self):
        self.net.enabled = False
        self.net.missing = ["DISCORD_BOT_TOKEN"]
        for source in self.net.sources.values():
            source.update(delayed=True, incident_since=time.time()-1000, error="HTTP 429 <unsafe>")
        text = self.request("/admin?key=fixture")[2]
        self.assertEqual(text.count('id="network-incidents"'), 1)
        markers = ["Chat relay: running", "Discovery network off", "Twitch data stale", "YouTube data stale"]
        positions = [text.index(x) for x in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Add it to ~/.livechat-xr.env on Dora, then restart the relay.", text)
        self.assertIn("HTTP 429 &lt;unsafe&gt;", text)
        self.net.enabled = True
        for source in self.net.sources.values():
            source.update(delayed=False, last_success=time.time(), error="")
        text = self.request("/admin?key=fixture")[2]
        self.assertNotIn('id="network-incidents"', text)
        self.assertIn("Discovery: all sources current", text)


class Identify(unittest.TestCase):
    def test_identity_uses_token_then_users_me_and_finite_timeout(self):
        responses = [dict(access_token="fixture", token_type="Bearer"), dict(id="7")]
        def response(*args, **kwargs):
            obj = mock.MagicMock()
            obj.__enter__.return_value.read.return_value = json.dumps(responses.pop(0))
            return obj
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=response) as request:
            self.assertEqual(server.discord_identify("fixture"), "7")
        self.assertEqual(request.call_args_list[1].args[0].full_url, "https://discord.com/api/users/@me")
        self.assertTrue(all(c.kwargs["timeout"] == 10 for c in request.call_args_list))
        body = urllib.parse.parse_qs(request.call_args_list[0].args[0].data.decode())
        self.assertTrue(body["redirect_uri"][0].endswith("/network/discord/callback"))

    def test_invalid_identity_and_transport_errors_do_not_return_owner(self):
        for identity in ({}, {"id": "handle"}, {"id": 7}):
            replies = [dict(access_token="fixture", token_type="Bearer"), identity]
            def response(*args, **kwargs):
                obj = mock.MagicMock()
                obj.__enter__.return_value.read.return_value = json.dumps(replies.pop(0))
                return obj
            with mock.patch.object(server.urllib.request, "urlopen", side_effect=response):
                with self.assertRaises(ValueError): server.discord_identify("fixture")
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): server.discord_identify("fixture")

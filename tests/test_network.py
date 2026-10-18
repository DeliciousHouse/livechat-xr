"""Discovery foundation: real SQLite, HTTP signatures and production scheduling."""
import asyncio
import contextlib
import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nacl.signing import SigningKey
from tests.test_server import server
import network


def config():
    return dict({name: "123" for name in network.REQUIRED}, DISCORD_CLIENT_ID="123",
                POP1_LIVE_APPLICATION_ID="123")


def row(handle="streamer"):
    return dict(platform_id="42", handle=handle, title="Population: ONE", viewers=2,
                stream_id="stream", game="Population: ONE")


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = network.Store(Path(self.tmp.name) / "listings.db")
        self.store.initialize()

    def test_removal_and_upsert_both_orders_and_rename(self):
        self.store.upsert_presence("twitch", [row()], 100)
        listing = self.store.channels()[0]["listing_id"]
        for remove_first in (True, False):
            with self.subTest(remove_first=remove_first):
                self.store.set_listing(listing, opted_in=True, review_status="reviewed", discord_id="7")
                entered, done = threading.Event(), threading.Event()
                def first():
                    entered.set()
                    if remove_first:
                        self.store.set_listing(listing, opted_in=False, review_status="removed")
                    else:
                        self.store.upsert_presence("twitch", [row("renamed")], 101)
                    done.set()
                def second():
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(done.wait(2))
                    if remove_first:
                        self.store.upsert_presence("twitch", [row("renamed")], 101)
                    else:
                        self.store.set_listing(listing, opted_in=False, review_status="removed")
                threads = [threading.Thread(target=f) for f in (first, second)]
                for t in threads: t.start()
                for t in threads: t.join(3)
                self.assertEqual(self.store.discovery(102), [])
                self.assertEqual(len(self.store.channels()), 1)
                self.assertEqual(self.store.channels()[0]["handle"], "renamed")
                self.assertEqual(self.store.listing(listing)["review_status"], "removed")
        self.store.initialize()  # additive/repeatable, never resurrects removed entries
        self.assertFalse(self.store.listing(listing)["opted_in"])

    def test_visibility_requires_consent_review_size_freshness(self):
        self.store.upsert_presence("twitch", [row()], 100)
        lid = self.store.channels()[0]["listing_id"]
        for consent, review in ((False, "reviewed"), (True, "pending"), (True, "removed")):
            self.store.set_listing(lid, opted_in=consent, review_status=review)
            self.assertEqual(self.store.discovery(101), [])
        self.store.upsert_presence("twitch", [row()], 100)
        self.store.set_listing(lid, opted_in=True)
        self.store.record_supplied_channel(lid, "twitch", "streamer")
        self.store.bind_verified_channel(lid, "twitch", "streamer", "42")
        self.store.set_listing(lid, review_status="reviewed")
        self.assertEqual(len(self.store.discovery(101)), 1)
        self.assertEqual(self.store.discovery(1901), [])
        self.store.upsert_presence("twitch", [dict(row(), viewers=None)], 200)
        self.assertEqual(self.store.discovery(201), [])
        self.store.upsert_presence("twitch", [dict(row(), viewers=11)], 200)
        self.assertEqual(self.store.discovery(201), [])
        self.store.upsert_presence("twitch", [], 201)
        self.assertEqual(self.store.discovery(202), [])

    def test_http_removal_contends_with_collector_transaction(self):
        self.store.upsert_presence("twitch", [row()], 100)
        lid = self.store.channels()[0]["listing_id"]
        store = self.store
        entered, release = threading.Event(), threading.Event()
        class Remove(BaseHTTPRequestHandler):
            def do_POST(self):
                entered.set()
                store.set_listing(lid, opted_in=False, review_status="removed")
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args): pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Remove)
        thread = threading.Thread(target=srv.serve_forever)
        thread.start()
        try:
            for http_first in (True, False):
                store.set_listing(lid, opted_in=True, review_status="reviewed")
                entered.clear()
                errors = []
                def remove():
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/remove", data=b"", timeout=3) as r:
                            self.assertEqual(r.status, 204)
                    except Exception as e: errors.append(e)
                locked = threading.Event()
                release.clear()
                original_connect = store.connect
                @contextlib.contextmanager
                def paused_connect():
                    with original_connect() as db:
                        def trace(sql):
                            target = "UPDATE listings SET" if http_first else "DELETE FROM live_streams"
                            if sql.startswith(target):
                                locked.set()
                                if not release.wait(3): raise TimeoutError("test writer stalled")
                        # Both removal and collector now acquire BEGIN IMMEDIATE themselves.
                        db.set_trace_callback(trace)
                        yield db
                def collect():
                    try: store.upsert_presence("twitch", [row("new")], 101)
                    except Exception as e: errors.append(e)
                request = threading.Thread(target=remove)
                collector = threading.Thread(target=collect, name="collector")
                with mock.patch.object(store, "connect", paused_connect):
                    first, second = (request, collector) if http_first else (collector, request)
                    first.start()
                    self.assertTrue(locked.wait(2))
                    second.start()
                    if not http_first: self.assertTrue(entered.wait(2))
                    release.set()
                    for worker in (first, second):
                        worker.join(3)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(store.discovery(102), [])
                self.assertEqual(store.listing(lid)["review_status"], "removed")
        finally:
            srv.shutdown()
            thread.join()
            srv.server_close()


class Runtime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.net = network.Network(Path(self.tmp.name), server.normalise_channel, config())
        self.net.config["DISCORD_PUBLIC_KEY"] = SigningKey.generate().verify_key.encode().hex()
        self.addCleanup(self.tmp.cleanup)
        self.addAsyncCleanup(self.net.stop)

    async def test_missing_each_config_is_off(self):
        for name in network.REQUIRED:
            cfg = dict(self.net.config)
            cfg.pop(name)
            net = network.Network(Path(self.tmp.name), server.normalise_channel, cfg)
            with self.assertLogs("network", level="ERROR") as logs:
                await net.start()
            self.assertFalse(net.enabled)
            self.assertEqual(net.snapshot()["missing"], [name])
            self.assertEqual(net.tasks, set())
            self.assertIn(name, " ".join(logs.output))

    async def test_start_stop_and_retry_do_not_refresh_success(self):
        calls = threading.Event()
        def broken():
            calls.set()
            raise ValueError("secret URL must not be logged")
        with mock.patch.object(self.net, "collect_twitch", broken), \
                mock.patch.object(self.net, "collect_youtube", return_value=[]), \
                mock.patch.object(self.net, "register_commands", new=mock.AsyncMock()):
            with self.assertLogs("network", level="WARNING") as logs:
                await self.net.start()
                self.assertTrue(await asyncio.to_thread(calls.wait, 2))
                for _ in range(100):
                    if self.net.snapshot()["sources"]["twitch"]["error"]: break
                    await asyncio.sleep(.01)
                self.assertEqual(self.net.snapshot()["sources"]["twitch"]["last_success"], 0)
                self.assertNotIn("secret URL", " ".join(logs.output))
            tasks = list(self.net.tasks)
            await self.net.stop()
            self.assertTrue(all(t.done() for t in tasks))
            self.assertFalse(self.net.enabled)

    async def test_discord_mentions_retry_and_closed_dm(self):
        await asyncio.to_thread(self.net.store.initialize)
        await asyncio.to_thread(self.net.store.upsert_presence, "twitch", [row()], time.time())
        lid = self.net.store.channels()[0]["listing_id"]
        self.net.store.set_listing(lid, discord_id="7")
        payload = network.message("@everyone <@&999> **fake** [x](https://evil.test)")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("@everyone", payload["content"])
        self.assertNotIn("<@&999>", payload["content"])
        self.assertIn("\\*", payload["content"])
        role_payload = network.message("@everyone <@&999>", viewer_role="123")
        self.assertEqual(role_payload["allowed_mentions"], {"parse": [], "roles": ["123"]})
        self.assertTrue(role_payload["content"].endswith("<@&123>"))
        with mock.patch.object(self.net, "_discord", side_effect=[(429, {"retry_after": .05}), (200, {})]) as request:
            start = time.perf_counter()
            await self.net.discord("POST", "/channels/123/messages", payload)
            self.assertGreaterEqual(time.perf_counter() - start, .05)
            self.assertEqual(request.call_count, 2)
        with mock.patch.object(self.net, "_discord", return_value=(403, {"code": 50007})) as request:
            self.assertFalse(await self.net.send_dm(lid, "hello"))
            self.assertFalse(await self.net.send_dm(lid, "hello again"))
            self.assertEqual(request.call_count, 1)
        self.assertTrue(self.net.store.listing(lid)["dm_closed"])

    async def test_discord_failure_diagnostics_are_bounded_and_secret_free(self):
        # protects: safe rejection evidence; fails_when: upstream text/code leaks or is lost;
        # no_claim: these synthetic errors do not identify the live Discord rejection.
        sentinel = "credential-SENTINEL"
        path = f"/webhooks/123/{sentinel}/messages/@original"
        self.net.config["DISCORD_BOT_TOKEN"] = sentinel
        errors = [({"code": 50035, "message": sentinel, "errors": {sentinel: sentinel}}, 50035),
                  ({"code": sentinel}, None), ({"code": True}, None), ({"code": -1}, None),
                  ({"code": 1.5}, None), ({"code": 2 ** 80}, None), ({"code": "50035"}, None),
                  ([sentinel], None), (None, None)]
        for body, expected in errors:
            raw = json.dumps(body).encode()
            with self.subTest(body=body), self.assertLogs("network", level="WARNING") as logs, \
                    mock.patch.object(network.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                        "https://discord.com/api/v10" + path, 400, sentinel,
                        {"Authorization": sentinel}, io.BytesIO(raw))):
                with self.assertRaises(network.HTTPFailure) as raised:
                    await self.net.discord("PATCH", path, network.authored(sentinel, ephemeral=True))
            error = raised.exception
            self.assertEqual(error.status, 400)
            self.assertEqual(error.discord_code, expected)
            self.assertEqual(error.operation, "edit_original")
            self.assertEqual(str(error), f"Discord operation=edit_original status=400 code={expected}")
            self.assertEqual(logs.output, ["WARNING:network:" + str(error)])
            self.assertNotIn(sentinel, " ".join(logs.output) + str(error) + repr(error))
        for raw in (b"not JSON credential-SENTINEL", b"\xff"):
            with self.subTest(raw=raw), self.assertLogs("network", level="WARNING") as logs, \
                    mock.patch.object(network.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                        "https://discord.com/" + sentinel, 502, sentinel, {}, io.BytesIO(raw))):
                with self.assertRaises(network.HTTPFailure) as raised:
                    await self.net.discord(sentinel, "/" + sentinel, None)
            self.assertEqual(str(raised.exception), "Discord operation=other status=502 code=None")
            self.assertEqual(logs.output, ["WARNING:network:" + str(raised.exception)])

    async def test_discord_operation_labels_and_retry_exhaustion(self):
        # protects: all shared callers and bounded retries; fails_when: paths become labels
        # or a malformed 429 escapes diagnostics; no_claim: no live Discord requests.
        for method, path, operation in (
                ("PUT", "/applications/123/guilds/123/commands", "register_commands"),
                ("POST", "/users/@me/channels", "open_dm"),
                ("POST", "/channels/123/messages", "create_message"),
                ("PATCH", "/channels/123/messages/456", "edit_message"),
                ("GET", "/channels/123/messages?limit=100&before=456", "list_messages"),
                ("POST", "/secret-SENTINEL", "other")):
            with self.subTest(operation=operation), self.assertLogs("network", level="WARNING") as logs, \
                    mock.patch.object(self.net, "_discord", return_value=(400, {"code": 50035})):
                with self.assertRaises(network.HTTPFailure) as raised:
                    await self.net.discord(method, path, None)
            self.assertEqual(str(raised.exception), f"Discord operation={operation} status=400 code=50035")
            self.assertEqual(logs.output, ["WARNING:network:" + str(raised.exception)])
        with self.assertLogs("network", level="WARNING") as logs, \
                mock.patch.object(self.net, "_discord", return_value=(429, ["secret-SENTINEL"])) as request, \
                mock.patch.object(network, "wait_retry", new=mock.AsyncMock()) as wait:
            with self.assertRaises(network.HTTPFailure) as raised:
                await self.net.discord("PATCH", "/webhooks/123/secret-SENTINEL/messages/@original", None)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(wait.await_args_list, [mock.call(60)] * 3)
        self.assertEqual(str(raised.exception), "Discord operation=edit_original status=429 code=None")
        self.assertEqual(logs.output, ["WARNING:network:" + str(raised.exception)])

    async def test_registration_is_private_and_idempotent(self):
        self.net.config["POP1_LIVE_APPLICATION_ID"] = "456"
        with mock.patch.object(self.net, "_discord", return_value=(200, network.COMMANDS)) as request:
            await self.net.register_commands()
            await self.net.register_commands()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])
        self.assertEqual(request.call_args.args[:2], ("PUT", "/applications/456/guilds/123/commands"))
        self.assertEqual([c["name"] for c in request.call_args.args[2]],
                         ["network-status", "live", "optin", "profile", "optout", "review", "remove"])
        self.assertNotIn("squad", [c["name"] for c in request.call_args.args[2]])

    async def test_missing_bot_application_id_never_falls_back_to_oauth(self):
        for value in (None, ""):
            cfg = dict(self.net.config)
            if value is None: cfg.pop("POP1_LIVE_APPLICATION_ID")
            else: cfg["POP1_LIVE_APPLICATION_ID"] = value
            net = network.Network(Path(self.tmp.name), server.normalise_channel, cfg)
            with mock.patch.object(net, "discord", new=mock.AsyncMock()) as discord, \
                    self.assertLogs("network", level="ERROR"):
                await net.start()
            self.assertFalse(net.enabled)
            self.assertEqual(net.snapshot()["missing"], ["POP1_LIVE_APPLICATION_ID"])
            self.assertEqual(net.tasks, set())
            discord.assert_not_awaited()

    async def test_stop_joins_inflight_sqlite_work(self):
        entered, release = threading.Event(), threading.Event()
        real_upsert = self.net.store.upsert_presence
        def slow_write(*args):
            entered.set()
            if not release.wait(3): raise TimeoutError("writer stalled")
            return real_upsert(*args)
        with mock.patch.object(self.net, "collect_twitch", return_value=[]), \
                mock.patch.object(self.net, "collect_youtube", return_value=[]), \
                mock.patch.object(self.net, "register_commands", new=mock.AsyncMock()), \
                mock.patch.object(self.net.store, "upsert_presence", slow_write):
            await self.net.start()
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            stopped = asyncio.create_task(self.net.stop())
            try:
                await asyncio.sleep(.05)
                self.assertFalse(stopped.done(), "shutdown must join the SQLite thread, not just its cancelled coroutine")
            finally:
                release.set()
                await stopped
            self.assertEqual(self.net.io_tasks, set())

    async def test_retry_wait_rechecks_early_wakeups(self):
        with mock.patch.object(network.time, "perf_counter", side_effect=[0, .01, .03, .05]), \
                mock.patch.object(network.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            await network.wait_retry(.05)
        self.assertEqual(sleep.await_count, 2)
        self.assertAlmostEqual(sleep.await_args_list[0].args[0], .04)
        self.assertAlmostEqual(sleep.await_args_list[1].args[0], .02)

    async def test_real_poll_success_failure_recovery_and_quota_backoff(self):
        await asyncio.to_thread(self.net.store.initialize)
        failure = network.HTTPFailure(403, quota=True)
        responses = [[row()], failure, [row("renamed")]]
        snapshots, delays = [], []
        async def sleep(delay):
            delays.append(delay)
            snapshots.append(self.net.snapshot())
            if len(delays) == 3: self.net.stopping.set()
        with mock.patch.object(self.net, "collect_twitch", side_effect=responses), \
                mock.patch.object(network.asyncio, "sleep", sleep):
            await self.net.poll("twitch")
        first, failed, recovered = [s["sources"]["twitch"] for s in snapshots]
        self.assertGreater(first["last_success"], 0)
        self.assertEqual(failed["last_success"], first["last_success"])
        self.assertEqual(failed["success_streak"], 0)
        self.assertEqual(recovered["success_streak"], 1)
        self.assertGreaterEqual(delays[1], 3600)
        self.assertEqual(self.net.store.channels()[0]["handle"], "renamed")

    async def test_identity_uses_normalizer_and_official_youtube_not_scrape(self):
        with mock.patch.object(network.chat, "resolve_identity", return_value={"id": "42", "handle": "renamed"}) as resolve:
            self.assertEqual((await self.net.resolve_identity("twitch", "https://twitch.tv/streamer"))["id"], "42")
            resolve.assert_called_once_with("twitch", "streamer")
        with mock.patch.object(self.net, "api", return_value={"items": [{"id": "UC" + "a" * 22, "snippet": {"customUrl": "@renamed"}}]}) as api, \
                mock.patch.object(network.chat, "_yt_get", side_effect=AssertionError("no scrape")):
            self.assertEqual((await self.net.resolve_identity("youtube", "@streamer"))["handle"], "@renamed")
            self.assertEqual(api.call_args.args[1]["forHandle"], "@streamer")


class Collectors(unittest.TestCase):
    def setUp(self):
        self.net = network.Network(Path("unused"), server.normalise_channel, config())

    def test_twitch_pages_permanent_ids_and_repeated_cursor(self):
        item = dict(user_id="42", user_login="NewName", id="live", title="Pop1", viewer_count=2, game_id="game")
        pages = [{"data": [{"id": "game", "name": network.GAME}]},
                 {"data": [item], "pagination": {"cursor": "next"}}, {"data": [item]}]
        with mock.patch.object(network.chat, "_twitch_access", return_value="synthetic") as token, \
                mock.patch.object(self.net, "api", side_effect=pages) as api:
            result = self.net.collect_twitch()
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["platform_id"], result[0]["handle"]), ("42", "newname"))
        self.assertEqual(api.call_args.args[1]["after"], "next")
        token.assert_called_once()
        pages[-1] = pages[1]
        with mock.patch.object(network.chat, "_twitch_access", return_value="synthetic"), \
                mock.patch.object(self.net, "api", side_effect=pages):
            with self.assertRaises(ValueError): self.net.collect_twitch()

    def test_youtube_pages_live_game_and_unknown_audience(self):
        def video(cid, title=network.GAME, **live):
            return dict(id=cid, snippet=dict(channelId=cid, title=title),
                        liveStreamingDetails=dict(actualStartTime="start", **live))
        pages = [{"items": [{"id": {"videoId": "v1"}}], "nextPageToken": "next"},
                 {"items": [video("UC1", concurrentViewers="2"), video("UC2", actualEndTime="end"),
                            video("UC3", title="Other Game"), video("UC4")]},
                 {"items": []}]
        with mock.patch.object(self.net, "api", side_effect=pages) as api:
            result = self.net.collect_youtube()
        self.assertEqual([(r["platform_id"], r["viewers"]) for r in result], [("UC1", 2), ("UC4", None)])
        self.assertEqual(api.call_args.args[1]["pageToken"], "next")
        self.assertEqual(api.call_args_list[0].args[1]["eventType"], "live")
        with mock.patch.object(network, "request_json", return_value=(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})):
            with self.assertRaises(network.HTTPFailure) as raised:
                self.net.collect_youtube()
        self.assertTrue(raised.exception.quota)

    def test_discord_request_headers_for_bot_and_followup_webhook(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{}'
        for method, path, payload in (
                ("PUT", "/applications/123/guilds/123/commands", network.COMMANDS),
                ("POST", "/channels/123/messages", network.message("hello")),
                ("POST", "/users/@me/channels", {"recipient_id": "123"}),
                ("PATCH", "/webhooks/123/synthetic/messages/@original", network.message("hello"))):
            with self.subTest(path=path), \
                    mock.patch.object(network.urllib.request, "urlopen", return_value=response) as request:
                self.assertEqual(self.net._discord(method, path, payload), (200, {}))
                req = request.call_args.args[0]
                self.assertIsInstance(req, urllib.request.Request)
                self.assertEqual(req.get_header("User-agent"), "DiscordBot (https://livechat.deliciouswines.org, 1.0)")
                self.assertEqual(req.get_header("Content-type"), "application/json")
                self.assertEqual(req.get_header("Authorization"),
                                 None if path.startswith("/webhooks/") else "Bot " + self.net.config["DISCORD_BOT_TOKEN"])
                self.assertEqual(req.full_url, "https://discord.com/api/v10" + path)
                self.assertEqual(req.get_method(), method)
                self.assertEqual(json.loads(req.data), payload)
                self.assertEqual(request.call_args.kwargs["timeout"], 10)

    def test_http_timeout_including_discord_and_token_refresh(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{}'
        with mock.patch.object(network.urllib.request, "urlopen", return_value=response) as request:
            self.net._discord("POST", "/channels/123/messages", network.message("hello"))
        self.assertEqual(request.call_args.kwargs["timeout"], 10)
        with mock.patch.dict(os.environ, TWITCH_CLIENT_ID="synthetic", TWITCH_CLIENT_SECRET="synthetic"), \
                mock.patch.object(network.chat, "_twitch_token", ("", 0)), \
                mock.patch.object(network.urllib.request, "urlopen", return_value=response) as request:
            response.__enter__.return_value.read.return_value = b'{"access_token":"synthetic","expires_in":3600}'
            network.chat._twitch_access()
        self.assertEqual(request.call_args.kwargs["timeout"], 10)


    def test_rate_limit_headers_and_bad_response_do_not_become_success(self):
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {"Retry-After": "901"}, io.BytesIO(b'{}'))
        with mock.patch.object(network.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(network.HTTPFailure) as raised:
                self.net.api("https://example.test", {})
        self.assertEqual(raised.exception.retry_after, 901)
        with mock.patch.object(network, "request_json", return_value=(200, {})):
            with self.assertRaises(KeyError): self.net.collect_youtube()


class Packaging(unittest.TestCase):
    def test_docker_copy_layout_import_and_network_version(self):
        root = Path(__file__).resolve().parents[1]
        docker = (root / "server/Dockerfile").read_text()
        self.assertIn("PyNaCl==1.6.0", docker)
        copies = next(line.split()[1:-1] for line in docker.splitlines() if line.startswith("COPY "))
        with tempfile.TemporaryDirectory() as tmp:
            for source in copies: shutil.copy2(root / source, tmp)
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            def version():
                return subprocess.check_output([sys.executable, "-c", "import server; print(server.RELAY_VERSION)"],
                                               cwd=tmp, env=env, text=True).strip()
            before = version()
            path = Path(tmp) / "network.py"
            path.write_text(path.read_text() + "\n# version probe\n")
            self.assertNotEqual(before, version())
            # Run the real production main/start/finally path from the flattened image layout.
            env = {k: v for k, v in env.items() if k not in network.REQUIRED}
            env.update(DATA_DIR=str(Path(tmp) / "data"), PORT="0")
            script = """
import asyncio
import server
server.loop.call_later(.1, server.loop.stop)
server.main()
assert server.discovery is not None and not server.discovery.enabled
assert not server.discovery.tasks
tasks = asyncio.all_tasks(server.loop)
for task in tasks: task.cancel()
server.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
server.loop.close()
"""
            result = subprocess.run([sys.executable, "-c", script], cwd=tmp, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Discovery off: missing", result.stderr)


class Interactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = SigningKey.generate()
        cfg = config()
        cfg["DISCORD_PUBLIC_KEY"] = self.key.verify_key.encode().hex()
        self.net = network.Network(Path(self.tmp.name), server.normalise_channel, cfg)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever)
        self.thread.start()
        self.net.loop = self.loop
        self.net.enabled = True
        self.patch = mock.patch.object(server, "discovery", self.net)
        self.patch.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http = threading.Thread(target=self.srv.serve_forever)
        self.http.start()

    def tearDown(self):
        self.srv.shutdown()
        self.http.join()
        self.srv.server_close()
        asyncio.run_coroutine_threadsafe(self.net.stop(), self.loop).result(3)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()
        self.patch.stop()
        self.tmp.cleanup()

    def post(self, body, *, signed=True, flip=False):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json"}
        if signed:
            headers.update({"X-Signature-Timestamp": timestamp,
                "X-Signature-Ed25519": self.key.sign(timestamp.encode() + body).signature.hex()})
        if flip: body += b" "
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}/discord/interactions", body, headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_signed_live_completion_request_and_safe_rejection(self):
        # protects: signed ACK -> real /live -> actual edit-original Request contract;
        # fails_when: PATCH/headers/body/privacy or rejection diagnostics regress;
        # no_claim: the local urlopen fake is not live Discord acceptance or a root fix.
        self.net.store.initialize()
        self.net.config["POP1_LIVE_APPLICATION_ID"] = "456"
        original_urlopen = urllib.request.urlopen
        async def drain():
            await asyncio.gather(*list(self.net.tasks))
        for status in (200, 400):
            requests = []
            def transport(req, *, timeout):
                if not req.full_url.startswith("https://discord.com/"):
                    return original_urlopen(req, timeout=timeout)
                requests.append((req, timeout, threading.get_ident()))
                if status != 200:
                    raise urllib.error.HTTPError(req.full_url, status, "reason-SENTINEL",
                        {"Authorization": "header-SENTINEL"}, io.BytesIO(
                            b'{"code":50035,"message":"body-SENTINEL"}'))
                response = mock.MagicMock()
                response.__enter__.return_value.status = 200
                response.__enter__.return_value.read.return_value = b'{}'
                return response
            body = dict(type=2, id=f"live{status}", application_id="456", token="token-SENTINEL",
                        guild_id="123", member={"user": {"id": "7"}}, data={"name": "live"})
            capture = self.assertNoLogs("network", level="WARNING") if status == 200 else self.assertLogs("network", level="WARNING")
            with self.subTest(status=status), capture as logs, \
                    mock.patch.object(network.urllib.request, "urlopen", side_effect=transport):
                self.assertEqual(self.post(body), (200, {"type": 5, "data": {"flags": 64}}))
                asyncio.run_coroutine_threadsafe(drain(), self.loop).result(4)
            self.assertEqual(len(requests), 1)
            req, timeout, worker = requests[0]
            self.assertIsInstance(req, urllib.request.Request)
            self.assertEqual(req.full_url, "https://discord.com/api/v10/webhooks/456/token-SENTINEL/messages/@original")
            self.assertEqual(req.get_method(), "PATCH")
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(req.get_header("User-agent"), "DiscordBot (https://livechat.deliciouswines.org, 1.0)")
            self.assertIsNone(req.get_header("Authorization"))
            self.assertEqual(timeout, 10)
            self.assertNotEqual(worker, self.thread.ident)
            payload = json.loads(req.data)
            self.assertIn("No small Pop1 streams live right now", payload["content"])
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertEqual(payload["flags"], 68)
            if status != 200:
                self.assertEqual(logs.output, [
                    "WARNING:network:Discord operation=edit_original status=400 code=50035",
                    "WARNING:network:Discord follow-up: HTTPFailure"])
                self.assertNotIn("SENTINEL", " ".join(logs.output))

    def test_real_signature_ping_and_invalid_payloads(self):
        self.assertEqual(self.post({"type": 1}), (200, {"type": 1}))
        self.assertEqual(self.post({"type": 1}, signed=False)[0], 401)
        self.assertEqual(self.post({"type": 1}, flip=True)[0], 401)
        for body in (b"{", b"[]", b"null"):
            self.assertEqual(self.post(body)[0], 400)

    def test_slow_command_ack_then_followup_and_scope(self):
        self.net.config["POP1_LIVE_APPLICATION_ID"] = "456"
        posted = threading.Event()
        received = []
        received_paths = []
        async def slow(data):
            await asyncio.sleep(3.1)
            return "finished"
        class Discord(BaseHTTPRequestHandler):
            def do_PATCH(self):
                received_paths.append(self.path)
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
                posted.set()
            def log_message(self, format, *args): pass
        discord = ThreadingHTTPServer(("127.0.0.1", 0), Discord)
        thread = threading.Thread(target=discord.serve_forever)
        thread.start()
        self.addCleanup(discord.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(discord.shutdown)
        def transport(method, path, payload):
            return network.request_json(f"http://127.0.0.1:{discord.server_port}" + path,
                                        data=payload, method=method)
        body = dict(type=2, id="interaction1", application_id="456", token="synthetic",
                    guild_id="123", member={"user": {"id": "7"}}, data={"name": "network-status"})
        with mock.patch.object(self.net, "command", slow), mock.patch.object(self.net, "_discord", transport):
            self.assertEqual(self.post(dict(body, application_id="123"))[0], 403)
            start = time.monotonic()
            self.assertEqual(self.post(body), (200, {"type": 5, "data": {"flags": 64}}))
            self.assertLess(time.monotonic() - start, 3)
            self.assertFalse(posted.is_set())
            self.assertTrue(posted.wait(4))
            self.assertEqual(received_paths, ["/webhooks/456/synthetic/messages/@original"])
            self.assertEqual(received[0]["content"], "finished")
            self.assertEqual(received[0]["flags"], 68)
            self.assertEqual(self.post(dict(body, guild_id="other"))[0], 403)
            dm = dict(body, id="dm", user={"id": "unauthorized"})
            dm.pop("guild_id")
            self.assertEqual(self.post(dm)[0], 403)
            self.assertEqual(self.post(dict(body, data={"name": "squad"}))[1]["type"], 4)


if __name__ == "__main__":
    unittest.main()

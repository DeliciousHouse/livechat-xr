"""E1/E2/E5 and DR8: real runtime, SQLite archives and local admin HTTP."""
import asyncio
import contextlib
import io
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tests.test_network import config, row, network, server
from nacl.signing import SigningKey


def evening(day="2026-10-17", hour=18, minute=0):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute,
        tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


class Incidents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = network.Network(self.tmp.name, server.normalise_channel, config())
        self.net.store.initialize()
        self.net.enabled = True
        self.now = evening()
        self.net.started_at = self.now
        self.net.notify_founder = mock.AsyncMock()

    async def test_independent_incidents_startup_repeats_and_two_good_polls(self):
        n, t = self.net, self.now
        n.record_poll("twitch", now=t + 899)
        await n.check_incidents(t + 899)
        self.assertFalse(n.snapshot()["sources"]["youtube"]["delayed"])
        await n.check_incidents(t + 900)
        state = n.snapshot()
        self.assertTrue(state["sources"]["youtube"]["delayed"])
        self.assertFalse(state["sources"]["twitch"]["delayed"])
        self.assertEqual(n.notify_founder.await_count, 1)
        await n.check_incidents(t + 901)
        self.assertEqual(n.notify_founder.await_count, 1)
        n.record_poll("youtube", now=t + 902)
        await n.check_incidents(t + 902)
        self.assertTrue(n.snapshot()["sources"]["youtube"]["delayed"])
        n.record_poll("youtube", error=RuntimeError("secret URL must not appear"), now=t + 903)
        self.assertNotIn("secret", str(n.snapshot()))
        n.record_poll("youtube", now=t + 904)
        await n.check_incidents(t + 904)
        self.assertTrue(n.snapshot()["sources"]["youtube"]["delayed"])
        n.record_poll("youtube", now=t + 905)
        await n.check_incidents(t + 905)
        self.assertFalse(n.snapshot()["sources"]["youtube"]["delayed"])
        self.assertEqual(n.notify_founder.await_count, 2)
        self.assertIn("recovered", n.notify_founder.call_args.args[0])
        # A detached snapshot is not a second mutable source of truth.
        state["sources"]["youtube"]["delayed"] = False
        await n.check_incidents(t + 1806)
        self.assertTrue(n.snapshot()["sources"]["youtube"]["delayed"])
        self.assertEqual(n.notify_founder.await_count, 4)  # both sources now stale

    async def test_window_boundaries_dst_and_disabled(self):
        for day in ("2026-03-08", "2026-11-01"):
            for hour, minute, active in ((17, 59, False), (18, 0, True), (22, 59, True), (23, 0, False)):
                with self.subTest(day=day, hour=hour, minute=minute):
                    n = network.Network(self.tmp.name, server.normalise_channel, config())
                    n.enabled = True
                    now = evening(day, hour, minute)
                    n.started_at = now - 901
                    n.notify_founder = mock.AsyncMock()
                    await n.check_incidents(now)
                    self.assertEqual(n.snapshot()["sources"]["twitch"]["delayed"], active)
        self.net.enabled = False
        await self.net.check_incidents(self.now + 900)
        self.net.notify_founder.assert_not_awaited()

    async def test_founder_transport_and_notification_failure_do_not_repeat(self):
        n = self.net
        del n.notify_founder
        n.discord = mock.AsyncMock(side_effect=[{"id": "7"}, {}])
        await n.notify_founder("YouTube stale — HTTP 429. /admin")
        self.assertEqual(n.discord.call_args_list[0].args[2], {"recipient_id": config()["POP1_LIVE_FOUNDER_ID"]})
        payload = n.discord.call_args_list[1].args[2]
        self.assertTrue(payload["content"].startswith("Pop1 Live:"))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        n.notify_founder = mock.AsyncMock(side_effect=RuntimeError("private"))
        await n.check_incidents(self.now + 900)
        await n.check_incidents(self.now + 901)
        self.assertEqual(n.notify_founder.await_count, 2)  # one attempt per source, never per tick

    async def test_runtime_maintenance_and_real_poll_use_same_state(self):
        n = self.net
        n.enabled = False
        n.config["DISCORD_PUBLIC_KEY"] = SigningKey.generate().verify_key.encode().hex()
        n.collect_twitch = mock.Mock(return_value=[])
        n.collect_youtube = mock.Mock(side_effect=network.HTTPFailure(429))
        n.register_commands = mock.AsyncMock()
        await n.start()
        try:
            for _ in range(100):
                if n.snapshot()["sources"]["twitch"]["last_success"]: break
                await asyncio.sleep(.01)
            state = n.snapshot()["sources"]
            self.assertGreater(state["twitch"]["last_success"], 0)
            self.assertEqual(state["youtube"]["error"], "HTTP 429")
            self.assertTrue(any(t.get_coro().__name__ == "maintain" for t in n.tasks))
        finally:
            await n.stop()


class Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = network.Store(Path(self.tmp.name) / "listings.db")
        self.store.initialize()
        self.now = time.time()
        self.store.upsert_presence("twitch", [row()], self.now)
        self.lid = self.store.channels()[0]["listing_id"]

    def enroll(self):
        self.store.record_supplied_channel(self.lid, "twitch", "streamer")
        self.store.bind_verified_channel(self.lid, "twitch", "streamer", "42")
        self.store.set_listing(self.lid, opted_in=True)
        self.store.set_listing(self.lid, review_status="reviewed")

    def test_provenance_conflicts_removal_reoptin_and_deadline(self):
        self.enroll()
        other = dict(row("other"), platform_id="99")
        self.store.upsert_presence("twitch", [row(), other], self.now)
        other_id = self.store.channels()[1]["listing_id"]
        with self.assertRaises(ValueError):
            self.store.record_supplied_channel(other_id, "twitch", "STREAMER")
        for platform, handle in (("tiktok", "streamer"), ("twitch", "bad handle"), ("youtube", "https://evil.test")):
            with self.assertRaises(ValueError): self.store.record_supplied_channel(self.lid, platform, handle)
        self.store.set_listing(self.lid, opted_in=False, now=self.now)
        self.assertEqual(self.store.discovery(self.now), [])
        self.assertIsNone(self.store.peak("twitch", "42", self.now))
        self.assertFalse(any(c["listing_id"] == self.lid for c in self.store.channels()))
        self.store.set_listing(self.lid, review_status="removed", now=self.now + 10)
        self.assertEqual(self.store.listing(self.lid)["removed_at"], self.now)
        self.store.set_listing(self.lid, opted_in=True, review_status="reviewed", now=self.now + 20)
        self.assertEqual(self.store.listing(self.lid)["review_status"], "pending")
        self.assertIsNone(self.store.listing(self.lid)["removed_at"])
        self.store.set_listing(self.lid, opted_in=False, now=self.now + 30)
        self.store.expire(self.now + 30 + 7 * 86400 - 1)
        self.assertIsNotNone(self.store.listing(self.lid))
        self.store.expire(self.now + 30 + 7 * 86400)
        self.assertIsNone(self.store.listing(self.lid))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM supplied_channels").fetchone()[0], 0)

    def test_live_end_24h_expiry_peak_unknown_and_restart(self):
        self.enroll()
        self.assertEqual(self.store.peak("twitch", "42", self.now), 2)
        self.store.upsert_presence("twitch", [dict(row(), viewers=7)], self.now + 1)
        self.store.upsert_presence("twitch", [dict(row(), viewers=None)], self.now + 2)
        self.assertEqual(self.store.discovery(self.now + 2), [])  # unknown is not zero
        self.assertEqual(self.store.peak("twitch", "42", self.now + 2), 7)
        self.store.upsert_presence("twitch", [], self.now + 3)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_streams").fetchone()[0], 0)
        self.assertEqual(self.store.peak("twitch", "42", self.now + 3), 7)
        self.assertIsNone(self.store.peak("twitch", "42", self.now + 1 + 86400))
        self.store.upsert_presence("twitch", [row()], self.now + 5)
        self.store.expire(self.now + 5 + 86400)
        self.assertEqual(self.store.channels(), [])
        self.assertIsNone(network.Store(self.store.path).peak("twitch", "42", self.now + 6))

    def test_migration_idempotent_no_provenance_guess_and_legacy_removal(self):
        path = Path(self.tmp.name) / "legacy.db"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript("""CREATE TABLE listings(id INTEGER PRIMARY KEY, discord_id TEXT NOT NULL DEFAULT '',
                opted_in INTEGER NOT NULL DEFAULT 0, review_status TEXT NOT NULL DEFAULT 'pending', dm_closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE channels(id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
                platform TEXT NOT NULL, platform_id TEXT NOT NULL, handle TEXT NOT NULL, UNIQUE(platform,platform_id));
                CREATE TABLE presence(channel_id INTEGER PRIMARY KEY REFERENCES channels(id), stream_id TEXT NOT NULL,
                title TEXT NOT NULL, viewers INTEGER, game TEXT NOT NULL, seen_at REAL NOT NULL, live INTEGER NOT NULL);
                INSERT INTO listings VALUES(1,'',0,'removed',0);
                INSERT INTO listings VALUES(2,'',1,'reviewed',0);
                INSERT INTO channels VALUES(1,2,'twitch','42','streamer');""")
            db.execute("INSERT INTO presence VALUES(1,'s','fixture',2,?,?,1)", (network.GAME, self.now))
            db.commit()
        store = network.Store(path)
        with mock.patch.object(store, "_expire", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError): store.initialize()
        with store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='presence'").fetchone())
        store.initialize()
        store.initialize()
        self.assertIsNone(store.listing(1))
        self.assertEqual(store.listing(2)["review_status"], "reviewed")
        with store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM supplied_channels").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='presence'").fetchone())
            self.assertEqual(db.execute("SELECT verified FROM channels").fetchone()[0], 0)
        self.assertEqual(store.discovery(), [])
        with store.connect() as db: db.execute("CREATE TABLE presence(bad TEXT)")
        with self.assertRaises(ValueError): store.initialize()
        with store.connect() as db: db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError): store.initialize()

    def test_unknown_primary_is_not_secondary_count_or_peak_fallback(self):
        self.enroll()
        self.store.upsert_presence("youtube", [dict(row(), platform_id="yt", viewers=2)], self.now)
        self.store.record_supplied_channel(self.lid, "youtube", "@streamer")
        self.store.bind_verified_channel(self.lid, "youtube", "@streamer", "yt")
        self.store.upsert_presence("twitch", [dict(row(), viewers=None)], self.now)
        self.assertEqual(self.store.discovery(self.now), [])
        self.assertEqual([r["viewers"] for r in self.store.live_channels(self.now)], [None, 2])
        self.assertEqual(self.store.peak("twitch", "42", self.now), 2)
        self.store.upsert_presence("twitch", [], self.now)
        self.assertEqual(self.store.discovery(self.now)[0]["platform"], "youtube")
        self.store.upsert_presence("youtube", [], self.now)
        self.assertEqual(self.store.discovery(self.now), [])

    def test_invalid_supplied_handles_and_duplicate_rebinding_conflicts(self):
        self.enroll()
        self.store.upsert_presence("twitch", [dict(row(), platform_id="another")], self.now)
        other = next(c["listing_id"] for c in self.store.channels() if c["platform_id"] == "another")
        self.store.record_supplied_channel(other, "twitch", "another")
        for platform, handle in (("tiktok", "name"), ("twitch", "bad/handle"), ("youtube", "UCid"), ("twitch", "")):
            with self.assertRaises(ValueError): self.store.record_supplied_channel(other, platform, handle)
        with self.assertRaises(ValueError):
            self.store.bind_verified_channel(other, "twitch", "another", "42")
        with self.assertRaises(ValueError):
            self.store.bind_verified_channel(self.lid, "twitch", "streamer", "another")
        self.assertEqual(next(c["listing_id"] for c in self.store.channels() if c["platform_id"] == "42"), self.lid)

    def test_expired_removal_cannot_reopt_before_maintenance(self):
        self.enroll()
        self.store.set_listing(self.lid, opted_in=False, now=self.now)
        with self.assertRaises(ValueError):
            self.store.set_listing(self.lid, opted_in=True, now=self.now + 7 * 86400)
        self.assertIsNone(self.store.listing(self.lid))


class AdminHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = network.Network(self.tmp.name, server.normalise_channel, {})
        self.net.store.initialize()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever)
        self.thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.srv.shutdown)
        for name, value in (("discovery", self.net), ("ADMIN_KEY", "fixture-admin"), ("regs", {}), ("logbuf", [])):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def get(self, path="/admin?key=fixture-admin"):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.srv.server_port}" + path, timeout=3) as response:
            return response.read().decode()

    def test_off_running_chat_healthy_stale_closed_and_auth(self):
        with mock.patch.object(server.loop, "is_running", return_value=True):
            page = self.get()
        self.assertEqual(page.count('id="network-incidents"'), 1)
        self.assertIn("Chat relay: running", page)
        self.assertIn("Discovery network off", page)
        self.assertIn("DISCORD_BOT_TOKEN", page)
        self.assertIn("~/.livechat-xr.env", page)
        self.assertNotIn("all sources current", page)
        for path in ("/admin", "/admin?key=wrong"):
            with self.assertRaises(urllib.error.HTTPError) as error: self.get(path)
            self.assertEqual(error.exception.code, 404)
        self.net.enabled = True
        self.net.missing = []
        with mock.patch.object(server.loop, "is_running", return_value=True): page = self.get()
        self.assertIn("waiting for first successful polls", page)
        for platform in network.INTERVALS: self.net.record_poll(platform)
        with mock.patch.object(server.loop, "is_running", return_value=True): page = self.get()
        self.assertNotIn('id="network-incidents"', page)
        self.assertIn("Discovery: all sources current", page)
        self.net.sources["youtube"]["last_success"] = time.time() - 901
        with mock.patch.object(server.loop, "is_running", return_value=True): page = self.get()
        self.assertNotIn("all sources current", page)
        self.net.store.upsert_presence("twitch", [row()], time.time())
        lid = self.net.store.channels()[0]["listing_id"]
        self.net.store.set_listing(lid, dm_closed=True)
        self.net.sources["youtube"].update(delayed=True, incident_since=time.time()-1000, error="<script>bad</script>")
        page = self.get()
        self.assertIn("Chat relay: not running", page)
        self.assertIn("YouTube data stale", page)
        self.assertIn("DMs closed: 1 listing", page)
        self.assertNotIn("<script>bad</script>", page)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", page)
        self.net.enabled = False
        self.assertLess(self.get().index("Discovery network off"), self.get().index("DMs closed"))

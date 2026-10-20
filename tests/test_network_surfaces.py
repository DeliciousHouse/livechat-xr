"""Private fixture transports, real SQLite and signed local HTTP; not live Discord acceptance."""
import asyncio
import contextlib
import io
import json
import shutil
import sqlite3
import tarfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tests import test_network as foundation
from tests.test_network import config, network, row, server
from tests.test_network_operations import evening
import backup


class Profiles(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = foundation.Runtime.asyncSetUp
    async def test_profile_validation_detachment_and_archive_restore(self):
        s = self.net.store
        s.initialize()
        lid = s.owner_listing("7", create=True)["id"]
        self.assertEqual(s.owner_listing("7", create=True)["id"], lid)
        s.set_listing(lid, opted_in=True)
        s.record_supplied_channel(lid, "twitch", "supplied")
        s.update_profile(lid, ign="Naiya", timezone="Europe/London", alerts_enabled=True, would_pay=True)
        s.update_profile(lid, would_pay=True)
        p = s.profile(lid)
        self.assertEqual((p["ign"], p["timezone"], p["would_pay"]), ("Naiya", "Europe/London", 1))
        p["ign"] = "changed"
        self.assertEqual(s.profile(lid)["ign"], "Naiya")
        for fields in ({"ign": ""}, {"ign": "x" * 65}, {"ign": "@everyone"}, {"ign": "x\n"},
                       {"ign": "x\u200b"}, {"timezone": "bad/zone"}, {"alerts_enabled": "false"}, {"would_pay": 2}):
            with self.subTest(fields=fields), self.assertRaises(ValueError): s.update_profile(lid, **fields)
        data = Path(self.tmp.name)
        (data / "registrations.json").write_text('{}')
        stream = io.BytesIO()
        backup.archive(data, stream)
        archive = data / "fixture.tar"
        archive.write_bytes(stream.getvalue())
        self.assertTrue(backup.check_archive(archive))
        with tarfile.open(fileobj=io.BytesIO(stream.getvalue())) as tar:
            manifest = json.load(tar.extractfile("data/network-backup.json"))
            self.assertEqual(manifest["schema"], 2)
        restored = data / "restored"
        restored.mkdir()
        shutil.copyfile(data / "listings.snapshot.db", restored / "listings.snapshot.db")
        backup.restore(restored, relay_stopped=True)
        other = network.Store(restored / "listings.db")
        other.initialize()
        self.assertEqual(other.profile(lid)["ign"], "Naiya")
        self.assertEqual(other.channels(), [])
        with other.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM supplied_channels")], [(lid, "twitch", "supplied")])
        self.assertEqual(s.demand(), {"total": 1, "outside_cohort": None})
        s.set_listing(lid, opted_in=False, now=time.time() - 7 * 86400)
        s.expire()
        self.assertIsNone(s.profile(lid))

    async def test_v1_migration_transaction_and_repeat(self):
        s = self.net.store
        s.initialize()
        # Reconstruct the exact v1 shape from v2, not a permissive fake schema.
        with s.connect() as db:
            for name in ("ign", "timezone", "alerts_enabled", "would_pay"):
                db.execute("ALTER TABLE listings DROP COLUMN " + name)
            db.execute("PRAGMA user_version=1")
        with mock.patch.object(s, "_expire", side_effect=RuntimeError("interrupt")):
            with self.assertRaises(RuntimeError): s.initialize()
        with s.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("ign", {r[1] for r in db.execute("PRAGMA table_info(listings)")})
        legacy = Path(self.tmp.name) / "legacy-restore"
        legacy.mkdir()
        with s.connect() as src, contextlib.closing(sqlite3.connect(legacy / "listings.snapshot.db")) as dst:
            src.backup(dst)
            dst.execute("DROP TABLE live_streams")
            dst.execute("DROP TABLE channels")
        self.assertEqual(backup.check_snapshot(legacy / "listings.snapshot.db"), 1)
        backup.restore(legacy, relay_stopped=True)
        restored = network.Store(legacy / "listings.db")
        restored.initialize()
        with restored.connect() as db: self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        s.initialize()
        s.initialize()
        with s.connect() as db: self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)


class Surfaces(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await foundation.Runtime.asyncSetUp(self)
        self.now = evening()
        # Keep implicit retention checks on the same clock as fixture observations.
        clock = mock.patch.object(network.time, "time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.net.store.initialize()
        self.net.enabled = True
        self.net.discord = mock.AsyncMock(return_value={"id": "message"})
        self.presences = {p: {} for p in network.INTERVALS}

    def enroll(self, owner="7", handle="streamer", pid="42", viewers=2, platform="twitch", lid=None):
        s = self.net.store
        lid = lid or s.owner_listing(owner, create=True)["id"]
        self.presences[platform][pid] = dict(row(handle), platform_id=pid, viewers=viewers, stream_id="abcdefghijk")
        s.upsert_presence(platform, list(self.presences[platform].values()), self.now)
        s.set_listing(lid, opted_in=True, dm_closed=False)
        s.record_supplied_channel(lid, platform, handle)
        s.bind_verified_channel(lid, platform, handle, pid)
        s.set_listing(lid, review_status="reviewed")
        s.update_profile(lid, ign="Naiya", alerts_enabled=True)
        return lid

    async def test_owner_lookup_expires_channels_at_retention_boundary(self):
        lid = self.enroll()
        with mock.patch.object(network.time, "time", return_value=self.now + 86399):
            self.assertEqual(self.net.store.owner_listing("7")["id"], lid)
            self.assertEqual(len(self.net.store.channels()), 1)
        with mock.patch.object(network.time, "time", return_value=self.now + 86400):
            self.assertEqual(self.net.store.owner_listing("7")["id"], lid)
            self.assertEqual(self.net.store.channels(), [])

    async def test_rows_states_staleness_and_overflow(self):
        n = self.net
        lid = self.enroll()
        self.enroll(handle="@streamer", platform="youtube", pid="UC" + "a" * 22, viewers=4, lid=lid)
        for p in network.INTERVALS: n.record_poll(p, now=self.now)
        text = (await n.render_live(self.now))["content"]
        self.assertIn("**Pop1 Live** · 1 small streams live · updated <t:", text)
        self.assertIn("**Naiya** · Twitch 2 · YouTube 4", text)
        self.assertIn("[Watch on Twitch](https://", text)
        self.assertIn("[Watch on YouTube](https://", text)
        self.assertNotIn("TikTok", text)
        self.assertIn("Live data from Twitch, YouTube · not affiliated", text)
        self.assertIn("`/live` anytime · `/follow pop1` for pings · `/optin` to get listed", text)
        self.assertEqual((await n.render_live(self.now))["flags"], 4)
        n.sources["youtube"].update(delayed=True, incident_since=self.now - 901)
        self.assertIn("⚠ YouTube streams delayed since", (await n.render_live(self.now))["content"])
        self.assertIn("last seen 18 min ago", (await n.render_live(self.now + 1080))["content"])
        n.enabled = False
        self.assertEqual((await n.render_live(self.now))["content"], "Pop1 Live is paused — back soon.")
        n.enabled = True
        self.assertIn("The list updates <t:", (await n.render_live(evening(hour=2), scheduled=True))["content"])
        self.assertIn("No small Pop1 streams live right now", (await n.render_live(self.now + 1801))["content"])
        # Large fixture uses the real renderer and keeps complete rows/footer within Discord's limit.
        for i in range(30):
            self.enroll(owner=str(100 + i), handle=f"name{i}", pid=str(100 + i))
        payload = await n.render_live(self.now)
        self.assertLessEqual(len(payload["content"]), 2000)
        self.assertRegex(payload["content"], r"\+\d+ more — /live for all")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    async def test_primary_unknown_and_matching_peak_reset_expiry(self):
        lid = self.enroll()
        s = self.net.store
        primary = s.live_channels(self.now)[0]
        self.assertTrue(network.similar_size(2, 4))
        self.assertTrue(network.similar_size(4, 0))
        self.assertTrue(network.similar_size(0, 4))
        self.assertTrue(network.similar_size(2, 0))
        self.assertFalse(network.similar_size(2, 5))
        self.assertTrue(network.similar_size(None, 4, offline=True))
        self.assertFalse(network.similar_size(None, 5, offline=True))
        self.assertFalse(network.similar_size(None, 2))
        self.assertEqual(s.matching_count(lid, self.now), (2, False))
        s.upsert_presence("twitch", [dict(row(), viewers=None)], self.now)
        self.assertEqual(s.matching_count(lid, self.now), (None, False))
        s.upsert_presence("twitch", [], self.now)
        self.assertEqual(s.matching_count(lid, self.now), (2, True))
        self.assertEqual(s.matching_count(lid, self.now + 86401), (None, True))
        self.assertEqual(network.Store(s.path).matching_count(lid, self.now), (None, True))
        s.upsert_presence("twitch", [dict(row(), viewers=1, stream_id="new")], self.now + 1)
        self.assertEqual(s.matching_count(lid, self.now + 1), (1, False))
        self.assertEqual(primary["viewers"], 2)

    async def test_post_schedule_removal_at_two_am_and_reconcile(self):
        n = self.net
        lid = self.enroll()
        n.record_poll("twitch", now=self.now)
        n.list_checked = True
        await n.surface_tick(self.now)
        self.assertEqual(n.discord.call_args.args[:2], ("POST", "/channels/123/messages"))
        await n.surface_tick(self.now + 299)
        self.assertEqual(n.discord.await_count, 1)
        await n.surface_tick(self.now + 300)
        self.assertEqual(n.discord.call_args.args[:2], ("PATCH", "/channels/123/messages/message"))
        n.naming_messages[("123", "ping")] = {lid}
        n.discord.reset_mock()
        with mock.patch.object(network.time, "time", return_value=evening(hour=2)):
            self.assertTrue(await n.remove_listing(lid))
        paths = [c.args[1] for c in n.discord.call_args_list]
        self.assertIn("/channels/123/messages/message", paths)
        self.assertIn("/channels/123/messages/ping", paths)
        self.assertTrue(any(network.REMOVED in c.args[2].get("content", "") for c in n.discord.call_args_list))
        n.store.upsert_presence("twitch", [row()], self.now)
        self.assertEqual(n.store.discovery(self.now), [])
        self.assertEqual(n.store.listing(lid)["review_status"], "removed")
        n.discord.side_effect = network.HTTPFailure(503)
        self.assertFalse(await n.remove_listing(lid))
        self.assertTrue(n.reconcile)
        n.discord.side_effect = None
        await n.surface_tick(evening(hour=2))
        self.assertFalse(n.reconcile)

    async def test_alert_edges_consent_closed_dm_and_no_self(self):
        n = self.net
        lid = self.enroll()
        other = self.enroll(owner="8", handle="other", pid="88")
        # Poll replaces a platform; put both channels in the same complete poll.
        n.store.upsert_presence("twitch", [dict(row(), stream_id="abcdefghijk"),
            dict(row("other"), platform_id="88", stream_id="otherstream")], self.now)
        with mock.patch.object(network.time, "time", return_value=self.now):
            await n.check_alerts()
            first = n.discord.await_count
            await n.check_alerts()
            self.assertEqual(n.discord.await_count, first)
            self.assertEqual(first, 4)  # two DMs; never self-alert
            self.assertTrue(any("just went live on Twitch with 2 viewers — about your size." in
                c.args[2].get("content", "") for c in n.discord.call_args_list))
            n.store.upsert_presence("twitch", [dict(row(), viewers=14, stream_id="larger1"),
                dict(row("other"), platform_id="88", viewers=15, stream_id="larger2")], self.now)
            self.assertEqual(n.store.discovery(self.now), [])
            await n.check_alerts()  # Free matching is not silently capped by small-list eligibility.
            self.assertEqual(n.discord.await_count, first + 4)
            first = n.discord.await_count
            n.store.set_listing(lid, dm_closed=True)
            n.store.update_profile(other, alerts_enabled=False)
            n.store.upsert_presence("twitch", [dict(row(), stream_id="new")], self.now + 1)
            await n.check_alerts()
            self.assertEqual(n.discord.await_count, first)

    async def test_official_proof_apis_no_search_and_exact_identity(self):
        n = self.net
        with mock.patch.object(network.chat, "_twitch_access", return_value="fixture"), \
             mock.patch.object(n, "api", side_effect=[{"data": [{"id": "42", "description": "code"}]},
                                                     {"data": [{"title": "code"}]}]) as api:
            self.assertTrue(await n.proof_visible("twitch", "streamer", "42", "code"))
            self.assertTrue(all("search" not in c.args[0] for c in api.call_args_list))
        with mock.patch.object(n, "api", side_effect=[{"items": [{"id": "UCx", "snippet": {"description": ""}}]},
            {"items": [{"snippet": {"channelId": "wrong", "title": "code"}, "liveStreamingDetails": {"actualStartTime": "yes"}}]}]):
            self.assertFalse(await n.proof_visible("youtube", "@streamer", "UCx", "code", ["abcdefghijk"]))

    async def test_authored_copy_and_external_fields_are_separate(self):
        n = self.net
        lid = self.enroll()
        n.store.update_profile(lid, ign="**false** [link](evil)")
        text = (await n.render_live(self.now))["content"]
        self.assertIn(r"**\*\*false\*\* \[link\]\(evil\)**", text)
        self.assertIn("[Watch on Twitch]", text)
        self.assertEqual(network.staged_copy("follow"), "Pop1 Live: Stopped. No more pings.")
        self.assertEqual(network.staged_copy("follow", enabled=True, times=(1, 2, 3)),
            "Pop1 Live: Following Pop1 Live — pings at <t:1:t>, <t:2:t>, <t:3:t>. Run /follow pop1 again to stop.")
        self.assertEqual(network.staged_copy("squad", until=42), "Pop1 Live: Looking for squad until <t:42:R> · [Stop looking]")
        self.assertEqual(network.staged_copy("already", until=42), "Pop1 Live: Already looking until <t:42:R>")
        self.assertEqual(network.staged_copy("offline"), "Pop1 Live: You need to be live in Pop1 to use /squad.")
        self.assertEqual(network.staged_copy("expired"), "Pop1 Live: This offer has ended.")
        ping = network.staged_ping([("Naiya", 1), ("Pop", 3), ("Brendan", 4)], "https://discord.com/channels/1/2/3", viewer_role="123")
        self.assertEqual(ping["content"].splitlines()[0], "Pop1 Live: 3 small streams live — Naiya (1), Pop (3), Brendan (4) <@&123>")
        self.assertEqual(ping["allowed_mentions"], {"parse": [], "roles": ["123"]})
        for text in (network.PENDING, network.APPROVED, network.REJECTED, network.REMOVED):
            n.discord.reset_mock()
            await n.send_dm(lid, text)
            self.assertEqual(n.discord.call_args.args[2]["content"], text)
        self.assertEqual(network.timezone_choices("Asia/Kolkata"), [{"name": "Asia/Kolkata", "value": "Asia/Kolkata"}])
        self.assertEqual(network.timezone_choices("fake"), [])

    async def test_actual_welcome_retry_only_success_clears_closed_dm(self):
        n = self.net
        lid = self.enroll()
        n.store.set_listing(lid, dm_closed=True)
        n.discord.side_effect = [{"dm_closed": True}]
        self.assertFalse(await n.welcome(lid))
        self.assertTrue(n.store.profile(lid)["dm_closed"])
        n.discord.side_effect = [{"id": "dm"}, {"dm_closed": True}]
        self.assertFalse(await n.welcome(lid))
        self.assertTrue(n.store.profile(lid)["dm_closed"])
        n.discord.side_effect = [{"id": "dm"}, {"id": "welcome"}]
        self.assertTrue(await n.welcome(lid))
        self.assertFalse(n.store.profile(lid)["dm_closed"])
        payload = n.discord.call_args.args[2]
        self.assertTrue(payload["content"].startswith(network.APPROVED))
        self.assertEqual(payload["components"][0]["components"][0]["label"], "Yes, I'd pay")
        owner = n.buttons[payload["components"][0]["components"][0]["custom_id"]][1]
        self.assertEqual(owner, "7")

    async def test_failed_cross_owner_proof_is_atomic_and_cancelled_proof_cannot_restore(self):
        n = self.net
        lid = self.enroll()
        challenge = dict(owner="8", platform="twitch", handle="streamer", platform_id="42",
                         deadline=time.monotonic() + 900, active=True)
        n.challenges["8"] = challenge
        with self.assertRaises(ValueError): n.finish_proof(challenge)
        self.assertIsNone(n.store.owner_listing("8"))
        self.assertEqual(n.store.owner_listing("7")["id"], lid)
        self.assertEqual(n.store.channels()[0]["listing_id"], lid)
        challenge.update(owner="7")
        n.challenges["7"] = challenge
        await n.remove_listing(lid)
        with self.assertRaises(ValueError): n.finish_proof(challenge)
        self.assertFalse(n.store.profile(lid)["opted_in"])
        n.store.upsert_presence("twitch", [row()], self.now)
        self.assertEqual(n.store.discovery(self.now), [])

    async def test_optout_races_proof_before_first_owner_commit(self):
        n = self.net
        challenge = dict(owner="7", platform="twitch", handle="streamer", platform_id="42",
                         deadline=time.monotonic() + 900, active=True)
        n.challenges["7"] = challenge
        entered, release = threading.Event(), threading.Event()
        bind = n.store.bind_verified_channel
        def paused_bind(*args, **kwargs):
            bind(*args, **kwargs)
            entered.set()
            if not release.wait(3): raise TimeoutError("test proof stalled")
        with mock.patch.object(n.store, "bind_verified_channel", paused_bind):
            proof = asyncio.create_task(n.offload(n.finish_proof, challenge))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                optout = asyncio.create_task(n.command(dict(type=2, guild_id="123", member={"user": {"id": "7"}}, data={"name": "optout"})))
                for _ in range(50):
                    if not challenge["active"]: break
                    await asyncio.sleep(.01)
                self.assertFalse(challenge["active"], "opt-out must cancel before waiting for the proof's SQLite lock")
            finally: release.set()
            with self.assertRaises(ValueError): await proof
            self.assertEqual((await optout)["content"], network.REMOVED)
        self.assertIsNone(n.store.owner_listing("7"))
        self.assertEqual(n.store.channels(), [])

    async def test_restart_reuses_only_own_list_and_stop_pauses(self):
        n = self.net
        n.config.update(DISCORD_CLIENT_ID="123", POP1_LIVE_APPLICATION_ID="456")
        n.discord.side_effect = [[
            {"id": "human", "author": {"id": "9"}, "content": "**Pop1 Live** fake"},
            {"id": "oauth-app", "author": {"id": "123"}, "content": "**Pop1 Live** wrong app"},
            {"id": "bot-ping", "author": {"id": "456"}, "content": "Pop1 Live: 3 small streams live"},
            {"id": "original", "author": {"id": "456"}, "content": "**Pop1 Live** · old list"}], {}]
        await n.surface_tick(self.now)
        self.assertEqual(n.live_message, ("123", "original"))
        self.assertEqual(n.discord.call_args.args[:2], ("PATCH", "/channels/123/messages/original"))
        n.discord.side_effect = None
        await n.stop()
        self.assertEqual(n.discord.call_args.args[2]["content"], "Pop1 Live is paused — back soon.")

    async def test_proof_same_reply_minute_checks_success_closed_dm_recovery(self):
        n = self.net
        body = dict(id="1", token="fixture", member={"user": {"id": "7"}}, guild_id="123",
                    data={"name": "optin", "options": [{"name": "platform", "value": "twitch"}, {"name": "handle", "value": "streamer"}]})
        n.resolve_identity = mock.AsyncMock(return_value={"id": "42", "handle": "streamer"})
        payload = await n.command(body)
        self.assertIn("Put this in your Twitch title or bio — I'll check every minute.", payload["content"])
        self.assertIn("```", payload["content"])
        self.assertNotIn("components", payload)
        challenge = n.challenges["7"]
        n.proof_visible = mock.AsyncMock(side_effect=[False, True])
        n.welcome = mock.AsyncMock(return_value=False)
        async def minute(_): return None
        with mock.patch.object(network, "wait_retry", minute):
            await n.check_challenge(challenge)
        edits = [c for c in n.discord.call_args_list if "@original" in c.args[1]]
        self.assertEqual(len(edits), 2)
        self.assertEqual(len({c.args[1] for c in edits}), 1)
        self.assertIn("Checking… not found yet (titles can take a minute)", edits[0].args[2]["content"])
        self.assertIn("Verified ✓ You can remove the code now.", edits[-1].args[2]["content"])
        self.assertIn(network.BLOCKED_DM, edits[-1].args[2]["content"])
        lid = n.store.owner_listing("7")["id"]
        self.assertEqual(n.store.listing(lid)["review_status"], "pending")
        self.assertEqual(n.store.discovery(), [])
        self.assertNotIn("7", n.challenges)
        n.welcome = mock.AsyncMock(return_value=True)
        retry = await n.command(body)
        self.assertIn("Verified ✓", retry["content"])
        n.welcome.assert_awaited_once_with(lid)

    async def test_proof_expiry_wrong_identity_replacement_and_cancellation(self):
        n = self.net
        body = dict(id="2", token="fixture", member={"user": {"id": "7"}}, guild_id="123",
                    data={"name": "optin", "options": [{"name": "platform", "value": "twitch"}, {"name": "handle", "value": "streamer"}]})
        n.resolve_identity = mock.AsyncMock(return_value={"id": "42", "handle": "streamer"})
        await n.command(body)
        old = n.challenges["7"]
        await n.command(body)
        self.assertIsNot(n.challenges["7"], old)
        n.resolve_identity.return_value = {"id": "99", "handle": "streamer"}
        challenge = n.challenges["7"]
        # Fast clock advances the production monotonic 15-minute state machine.
        clock = [0]
        challenge["deadline"] = 900
        async def minute(seconds): clock[0] += seconds
        with mock.patch.object(network.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(network, "wait_retry", minute):
            await n.check_challenge(challenge)
        edits = [c.args[2]["content"] for c in n.discord.call_args_list if "@original" in c.args[1]]
        self.assertEqual(len(edits), 15)
        self.assertIn("That code is on a different channel than @streamer", edits[0])
        self.assertEqual(edits[-1], "Code expired — run /optin again.")
        self.assertIsNone(n.store.owner_listing("7"))
        await n.command(body)
        await n.stop()
        self.assertEqual(n.challenges, {})


class SignedSurfaces(unittest.TestCase):
    tearDown = foundation.Interactions.tearDown
    post = foundation.Interactions.post
    def setUp(self):
        foundation.Interactions.setUp(self)
        self.net.store.initialize()
        self.sequence = 0

    def body(self, name="profile", owner="7", **kwargs):
        self.sequence += 1
        return dict(type=2, id=f"fixture{self.sequence}", application_id="123", token="fixture",
            guild_id="123", member={"user": {"id": owner}}, data={"name": name}, **kwargs)

    def test_signed_autocomplete_buttons_replay_and_founder_ownership(self):
        body = self.body()
        body.update(type=4, data={"name": "profile", "options": [{"name": "tz", "value": "los", "focused": True}]})
        status, reply = self.post(body)
        self.assertEqual((status, reply["type"]), (200, 8))
        self.assertIn({"name": "Los Angeles (America/Los_Angeles)", "value": "America/Los_Angeles"}, reply["data"]["choices"])
        self.assertEqual(self.post(body)[0], 409)
        lid = self.net.store.owner_listing("7", create=True)["id"]
        button = self.net.demand_button(lid, "7")["components"][0]["custom_id"]
        wrong = self.body(owner="8")
        wrong.update(type=3, data={"custom_id": button})
        self.assertEqual(self.post(wrong)[0], 403)
        good = self.body()
        good.update(type=3, data={"custom_id": button})
        done = __import__('threading').Event()
        async def transport(*args): done.set(); return {}
        with mock.patch.object(self.net, "discord", transport):
            self.assertEqual(self.post(good)[1]["type"], 5)
            self.assertTrue(done.wait(2))
        self.assertEqual(self.net.store.profile(lid)["would_pay"], 1)
        self.assertEqual(self.post(good)[0], 409)
        done.clear()
        with mock.patch.object(self.net, "discord", transport):
            self.assertEqual(self.post(dict(good, id="fresh"))[1]["type"], 5)  # idempotent tap
            self.assertTrue(done.wait(2))
        review = self.body("review")
        review["data"]["options"] = [{"name": "listing", "value": lid}, {"name": "decision", "value": "approve"}]
        async def command(): return await self.net.command(review)
        with self.assertRaises(PermissionError): asyncio.run_coroutine_threadsafe(command(), self.loop).result(2)
        review["member"]["user"]["id"] = "123"
        # Founder may review another owner's listing; channel ownership is separate.
        asyncio.run_coroutine_threadsafe(command(), self.loop).result(2)
        self.assertEqual(self.net.store.listing(lid)["review_status"], "reviewed")
        self.assertEqual(self.net.store.listing(lid)["discord_id"], "7")

    def test_real_signed_http_optin_profile_review_live_optout_and_forgery(self):
        n = self.net
        events = []
        condition = threading.Condition()
        def transport(method, path, payload):
            if method == "GET": return 200, []
            with condition:
                events.append((method, path, payload))
                condition.notify_all()
            return 200, {"id": "fixture-message"}
        def wait_for(text):
            with condition:
                return condition.wait_for(lambda: any(text in e[2].get("content", "") for e in events), timeout=4)
        async def minute(_): await asyncio.sleep(.01)
        n.resolve_identity = mock.AsyncMock(return_value={"id": "42", "handle": "streamer"})
        n.proof_visible = mock.AsyncMock(return_value=True)
        with mock.patch.object(n, "_discord", transport), mock.patch.object(network, "wait_retry", minute):
            body = self.body("optin")
            body["data"]["options"] = [{"name": "platform", "value": "twitch"}, {"name": "handle", "value": "streamer"}]
            self.assertEqual(self.post(body, flip=True)[0], 401)
            self.assertEqual(self.post(dict(body, application_id="other"))[0], 403)
            self.assertEqual(self.post(dict(body, guild_id="other"))[0], 403)
            self.assertEqual(self.post(body)[1]["type"], 5)
            self.assertTrue(wait_for("Verified ✓"))
            lid = n.store.owner_listing("7")["id"]
            self.assertEqual(n.store.profile(lid)["review_status"], "pending")
            original = [e for e in events if "/messages/@original" in e[1]]
            self.assertGreaterEqual(len(original), 2)
            self.assertEqual(len({e[1] for e in original}), 1)
            self.assertTrue(all(e[2]["flags"] == 68 for e in original))
            profile = self.body("profile")
            profile["data"]["options"] = [{"name": "ign", "value": "Naiya"}, {"name": "tz", "value": "Europe/London"}, {"name": "alerts", "value": True}]
            self.assertEqual(self.post(profile)[1]["type"], 5)
            self.assertTrue(wait_for("IGN: Naiya"))
            forged = self.body("profile", owner="8")
            forged["data"]["options"] = [{"name": "listing", "value": lid}, {"name": "discord_id", "value": "7"}]
            self.assertEqual(self.post(forged)[1]["type"], 5)
            self.assertTrue(wait_for("couldn't finish"))
            self.assertIsNone(n.store.owner_listing("8"))
            review = self.body("review", owner="123")
            review["data"]["options"] = [{"name": "listing", "value": lid}, {"name": "decision", "value": "approve"}]
            self.assertEqual(self.post(review)[1]["type"], 5)
            self.assertTrue(wait_for(network.APPROVED))
            n.store.upsert_presence("twitch", [row()], time.time())
            self.assertEqual([r["listing_id"] for r in n.store.discovery()], [lid])
            live = self.body("live")
            self.assertEqual(self.post(live)[1]["type"], 5)
            self.assertTrue(wait_for("[Watch on Twitch]"))
            self.assertEqual(self.post(self.body("optout"))[1]["type"], 5)
            self.assertTrue(wait_for(network.REMOVED))
            self.assertEqual(n.store.discovery(), [])
            self.assertFalse(n.challenges)
            expired = self.body()
            expired.update(type=3, data={"custom_id": "no-such-button"})
            self.assertEqual(self.post(expired)[1]["data"]["content"], "Pop1 Live: This offer has ended.")

    def test_actual_go_http_noncollection_expiry_restart_removal_and_open_redirects(self):
        n = self.net
        s = n.store
        s.upsert_presence("twitch", [row()], time.time())
        lid = s.channels()[0]["listing_id"]
        s.record_supplied_channel(lid, "twitch", "streamer")
        s.bind_verified_channel(lid, "twitch", "streamer", "42")
        s.set_listing(lid, opted_in=True, review_status="reviewed")
        channel = s.live_channels()[0]
        opaque = n.go_link(channel).rsplit("/", 1)[1]
        self.assertFalse(n.go[opaque]["posted"])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args): return None
        opener = urllib.request.build_opener(NoRedirect)
        def get(key):
            try: return opener.open(f"http://127.0.0.1:{self.srv.server_port}/go/{key}", timeout=3)
            except urllib.error.HTTPError as exc: return exc
        for _ in range(3):
            with mock.patch.object(server.log, "info") as logged:
                response = get(opaque)
                logged.assert_not_called()
            self.assertEqual(response.code, 302)
            self.assertEqual(response.headers["Location"], "https://www.twitch.tv/streamer")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertIsNone(response.headers.get("Set-Cookie"))
            response.close()
        for url in ("https://evil.test", "https://twitch.tv.evil.test/a", "https://user@twitch.tv/a", "https://twitch.tv/a#x",
                    "https://twitch.tv/a?url=evil", "https://youtube.com/watch?v=abcdefghijk&next=evil"):
            self.assertIsNone(network.safe_destination(url))
        self.assertEqual(get("invalid").code, 404)
        self.assertEqual(get(opaque + "?url=https://evil.test").code, 404)
        with mock.patch.object(network.time, "time", return_value=time.time() + 86401):
            self.assertEqual(get(opaque).code, 404)
        self.assertIsNone(network.Network(self.tmp.name, server.normalise_channel, config()).resolve_go(opaque))
        s.set_listing(lid, opted_in=False)
        self.assertEqual(get(opaque).code, 404)
        with s.connect() as db:
            self.assertFalse(any("visit" in r[0] or "click" in r[0] for r in db.execute("SELECT name FROM sqlite_master")))
        self.assertNotIn("visitors", n.__dict__)


if __name__ == "__main__": unittest.main()

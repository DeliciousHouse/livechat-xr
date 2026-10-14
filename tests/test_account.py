"""Account and rename regressions; synthetic registrations, no external traffic."""
import asyncio
import json
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests import test_server as fixture

chat, server, HOOK = fixture.chat, fixture.server, fixture.HOOK


class Account(unittest.TestCase):
    setUp = fixture.Server.setUp
    tearDown = fixture.Server.tearDown
    post = fixture.Server.post

    def register(self, **fields):
        self.post("/register", name="Ann", email="ann@example.com", twitch="someone", webhook=HOOK, **fields)
        return next(iter(server.regs))

    def test_edit_preserves_entitlements_and_revalidates_new_webhook(self):
        token = self.register()
        for plan in ("founder", "paid", "free"):
            r = server.regs[token]
            r.update(plan=plan, subscription="sub_test", created=123)
            before_founders = set(server.founders)
            new_hook = HOOK[:-1] + "b"
            with mock.patch.object(server, "check_webhook", return_value="New Discord") as check:
                code, headers, _ = self.post(f"/m/{token}/save", name="New", email="new@example.com",
                                             youtube="@newname", webhook=new_hook)
            self.assertEqual(code, 303)
            self.assertEqual(headers["Location"], f"/m/{token}?saved=1")
            check.assert_called_once_with(new_hook)
            r = server.regs[token]
            self.assertEqual((len(server.regs), r["plan"], r["subscription"], r["created"]), (1, plan, "sub_test", 123))
            self.assertEqual(server.founders, before_founders)
            self.assertEqual(self.started[-1], token)
            self.assertEqual(self.posts[-1][0], new_hook)
            self.assertIn(f"/m/{token}", self.posts[-1][1])
            self.assertEqual(json.loads((server.DATA / "registrations.json").read_text())[token], r)

    def test_platform_icons_default_on_and_toggle_from_settings(self):
        token = self.register()
        self.assertTrue(server.regs[token]["platform_icons"])
        self.assertIn('name="platform_icons" value="1" checked', urllib.request.urlopen(f"{self.base}/m/{token}").read().decode())
        form = dict(name="Ann", email="ann@example.com", twitch="someone", icons_form="1")
        with mock.patch.object(server, "check_webhook", return_value="Discord"):
            self.assertEqual(self.post(f"/m/{token}/save", **form)[0], 303)  # unchecked box: field absent
            self.assertFalse(server.regs[token]["platform_icons"])
            self.assertNotIn('value="1" checked', urllib.request.urlopen(f"{self.base}/m/{token}").read().decode())
            self.assertEqual(self.post(f"/m/{token}/save", platform_icons="1", **form)[0], 303)
        self.assertTrue(server.regs[token]["platform_icons"])

    def test_edit_validation_matches_register_and_leaves_record_unchanged(self):
        token = self.register()
        before = dict(server.regs[token])
        for hook, error in (("https://evil.test", None), (HOOK, ValueError("rejected"))):
            with mock.patch.object(server, "check_webhook", side_effect=error, return_value="Discord"):
                form = dict(name="Ann", email="ann@example.com", twitch="someone", webhook=hook)
                a = self.post("/register", **form)
                b = self.post(f"/m/{token}/save", **form)
            self.assertEqual((a[0], b[0]), (400, 400))
            message = "That isn&#x27;t a Discord webhook URL" if error is None else "Discord didn&#x27;t accept that webhook"
            self.assertIn(message, a[2])
            self.assertIn(message, b[2])
            self.assertIn(f'action="/m/{token}/save"', b[2])
            self.assertEqual(server.regs[token], before)
        self.check_channel.return_value = False
        self.assertEqual(self.post(f"/m/{token}/save", name="Ann", email="ann@example.com", twitch="missing", webhook=HOOK)[0], 400)
        self.assertEqual(server.regs[token], before)

    def test_blank_webhook_keeps_secret_out_of_page_and_rechecks_current(self):
        token = self.register()
        body = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertNotIn(HOOK, body)
        self.assertIn("Changing your Discord username needs no action", body)
        with mock.patch.object(server, "check_webhook", return_value="Discord") as check:
            self.assertEqual(self.post(f"/m/{token}/save", name="Ann", email="ann@example.com", twitch="someone")[0], 303)
        check.assert_called_once_with(HOOK)

    def test_recovery_requires_verified_identity_and_handles_duplicate_email(self):
        token = self.register()
        with mock.patch.object(server, "GOOGLE_ID", "configured"), \
                mock.patch.object(server, "google_identity", side_effect=ValueError):
            a = self.post("/recover", google="forged", email="ann@example.com")
            b = self.post("/recover", google="forged", email="absent@example.com")
            self.assertEqual((a[0], a[2]), (b[0], b[2]))
            self.assertNotIn(token, a[2])
        with mock.patch.object(server, "GOOGLE_ID", "configured"), \
                mock.patch.object(server, "google_identity", return_value=("Ann", "ann@example.com")):
            code, headers, _ = self.post("/recover", google="verified")
            self.assertEqual((code, headers["Location"]), (303, f"/m/{token}"))
            other = "otherregistrationtoken123"
            server.regs[other] = dict(server.regs[token])
            code, _, body = self.post("/recover", google="verified")
            self.assertEqual(code, 200)
            self.assertIn(f"/m/{token}", body)
            self.assertIn(f"/m/{other}", body)
        with mock.patch.object(server, "GOOGLE_ID", ""), mock.patch.object(server, "google_identity") as verify:
            self.assertEqual(self.post("/recover", google="verified")[0], 400)
            verify.assert_not_called()

    def test_diagnostic_allowlist_never_copies_private_error_text(self):
        token = self.register()
        server.stats[token] = {"last_post": 123, "last_error": f"{HOOK} {token} ann@example.com Fan: secret chat"}
        server.status[token] = f"Twitch: retrying ({HOOK} {token} ann@example.com Fan: secret chat)"
        code, _, body = self.post(f"/m/{token}/report")
        self.assertEqual(code, 200)
        payload = (server.DATA / "support.jsonl").read_text()
        for private in (HOOK, token, "ann@example.com", "Fan: secret chat"):
            self.assertNotIn(private, payload)
        row = json.loads(payload)
        self.assertEqual(row["channels"][0]["platform"], "twitch")
        self.assertEqual(row["channels"][0]["last_error"], "relay_error")
        self.assertIn("version", row)
        self.assertIn("uptime_seconds", row)
        self.assertIn("Diagnostic saved", body)
        self.post(f"/m/{token}/report")
        self.assertEqual(len((server.DATA / "support.jsonl").read_text().splitlines()), 1)

    def test_status_copy_for_each_channel(self):
        token = self.register()
        cases = [("connected to #someone", "Live: last message sent to Discord 2 min ago"),
                 ("waiting for someone to go live", "Waiting: you&#x27;re not live on Twitch right now"),
                 ("can't find someone", "That channel name wasn&#x27;t found. Check the spelling below."),
                 ("retrying (HTTP Error 429)", "Platform is rate-limiting us; retrying automatically"),
                 ("retrying (TimeoutError)", "Relay problem on our side; we&#x27;ve been notified")]
        server.stats[token] = {"channels": {"twitch": {"last_post": time.time() - 125}}}
        for raw, text in cases:
            server.status[token] = "Twitch: " + raw
            server.source_status(token, 'twitch', 'Twitch: ' + raw)
            body = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
            self.assertIn(text, body)
        server.stats[token]["discord_error"] = 404
        body = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertIn("Discord rejected the webhook. Paste a new one below.", body)
        server.regs[token].update(plan="free", channels={"twitch": "someone", "youtube": "@other"})
        body = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertIn("Waiting for upgrade", body)

    def test_relay_records_http_rejection_then_success_only_for_emitting_source(self):
        token = self.register()
        server.regs[token]['channels'] = {'twitch': 'someone', 'youtube': '@other'}
        async def relay(*args, **kwargs):
            kwargs['on_source_status']('twitch', 'Twitch: connected to #someone')
            kwargs['on_source_status']('youtube', 'YouTube: waiting for @other to go live')
            kwargs['on_lines'](['Fan: fixture chat'], ['twitch'])
            await asyncio.wait_for(attempt.wait(), 2)
            async def recorded():
                while not server.stats[token].get('fails' if rejecting else 'posts'):
                    await asyncio.sleep(.001)
            await asyncio.wait_for(recorded(), 2)
            raise asyncio.CancelledError
        def rejected(url, text, *, on_error, on_retry):
            on_error(401)
            event_loop.call_soon_threadsafe(attempt.set)
            return False
        def accepted(*args, **kwargs):
            event_loop.call_soon_threadsafe(attempt.set)
            return True
        async def run():
            nonlocal attempt, event_loop
            attempt, event_loop = asyncio.Event(), asyncio.get_running_loop()
            await server.run(token)
        attempt, event_loop = None, None
        rejecting = True
        with mock.patch.object(chat, 'relay', relay):
            with mock.patch.object(chat, 'post_discord', rejected):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(run())
            self.assertEqual(server.stats[token]['fails'], 1)
            self.assertIn('Discord rejected the webhook', server.channel_status(token, 'twitch'))
            self.assertEqual(server.diagnostic(token)['channels'][0]['last_error'], 'discord_rejected')
            rejecting = False
            with mock.patch.object(chat, 'post_discord', side_effect=accepted):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(run())
        self.assertEqual(server.stats[token]['posts'], 1)
        self.assertEqual(server.stats[token]['discord_error'], 0)
        self.assertGreater(server.channel_state(token, 'twitch')['last_post'], 0)
        self.assertNotIn('last_post', server.channel_state(token, 'youtube'))
        self.assertEqual(server.stats[token]['lines_posted'], 1)
        self.assertEqual(server.stats[token]['lines_dropped'], 0)

    def test_delivery_counts_in_account_status(self):
        token = self.register()
        server.stats[token] = dict(lines_received=214, lines_posted=214, lines_dropped=0)
        body = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertIn("All 214 chat messages delivered.", body)
        server.stats[token].update(lines_received=220, lines_dropped=2, dropped_backlog=2)
        body = urllib.request.urlopen(f"{self.base}/m/{token}").read().decode()
        self.assertIn("214 of 220 chat messages delivered; 4 queued; 2 older messages collapsed.", body)

    def test_relay_log_is_a_post_timeline_without_chat_text(self):
        token = self.register()

        async def relay(*args, on_lines=None, **kwargs):
            on_lines(["Ann: secret words", "Bob: more secret words"], ["tiktok"])
            await asyncio.sleep(0.2)  # the queue worker posts in the same loop
            raise asyncio.CancelledError

        with mock.patch.object(chat, "relay", relay), mock.patch.object(chat, "post_discord", lambda *a, **k: True), \
                self.assertLogs("relay", "INFO") as cm:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(server.run(token))
        timeline = [line.rsplit(": ", 1)[1] for line in cm.output if "lines" in line]
        self.assertEqual(timeline, ["2 lines queued", "posted 2 lines"])
        self.assertFalse(any("secret" in line for line in cm.output), "chat text must not be logged")

    def test_webhook_collision_and_stale_identity_cannot_overwrite_account(self):
        token = self.register()
        old = server.regs[token]
        other_hook = HOOK[:-1] + 'z'
        other = 'otherregistrationtoken123'
        server.regs[other] = {**old, 'webhook': other_hook}
        self.assertEqual(self.post(f'/m/{token}/save', name='Ann', email='ann@example.com',
                                   twitch='someone', webhook=other_hook)[0], 400)
        self.assertIs(server.regs[token], old)
        server.regs[token] = {**old, 'channels': {'twitch': 'edited'}}
        server.remember_identity(token, 'twitch', {'id': '123', 'handle': 'stale'}, expected=old)
        self.assertEqual(server.regs[token]['channels']['twitch'], 'edited')


class Identity(unittest.TestCase):
    def test_tiktok_capture_uses_room_owner_without_a_public_handle_lookup(self):
        from TikTokLive.events import ConnectEvent
        handlers, found = {}, []
        client = mock.Mock(connected=False, room_info={'owner': {'id': '123', 'sec_uid': 'fixture-sec-id'}})
        client.on = lambda cls: lambda fn: handlers.setdefault(cls, fn)
        async def connect(**kwargs):
            await handlers[ConnectEvent](mock.Mock())
            raise asyncio.CancelledError
        client.connect = connect
        with mock.patch('TikTokLive.TikTokLiveClient', return_value=client), \
                mock.patch.object(chat, 'resolve_identity') as lookup:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.tiktok('@someone', lambda _: None, lambda _: None, identity={}, on_identity=found.append))
        self.assertEqual(found, [{'id': '123', 'sec_uid': 'fixture-sec-id', 'handle': '@someone'}])
        lookup.assert_not_called()

    def test_twitch_connection_follows_renamed_login_and_captures_room_id(self):
        async def go():
            reader = mock.Mock(readline=mock.AsyncMock(side_effect=[
                b'@room-id=123 :tmi.twitch.tv ROOMSTATE #newname\r\n',
                b':fan!fan@host PRIVMSG #newname :hello\r\n']))
            writer = mock.Mock(drain=mock.AsyncMock(), wait_closed=mock.AsyncMock())
            statuses, identities, lines = [], [], []
            def emit(line):
                lines.append(line)
                raise asyncio.CancelledError
            with mock.patch.object(chat, 'resolve_identity', return_value={'id': '123', 'handle': 'newname'}), \
                    mock.patch('asyncio.open_connection', return_value=(reader, writer)):
                with self.assertRaises(asyncio.CancelledError):
                    await chat.twitch('oldname', emit, statuses.append, identity={'id': '123'}, on_identity=identities.append)
            self.assertIn(b'JOIN #newname', writer.write.call_args.args[0])
            self.assertIn(b'twitch.tv/commands', writer.write.call_args.args[0])
            self.assertEqual(lines, ['fan: hello'])
            self.assertIn('Twitch: connected to #newname', statuses)
            self.assertEqual(identities[-1]['id'], '123')
            writer.close.assert_called_once()
        asyncio.run(go())

    def test_youtube_connection_uses_permanent_channel_not_stale_handle(self):
        cid = 'UC' + 'a' * 22
        async def stop(_):
            raise asyncio.CancelledError
        with mock.patch.object(chat, 'resolve_identity', return_value={'id': cid, 'handle': '@renamed'}), \
                mock.patch.object(chat, '_yt_get', return_value='offline') as get, mock.patch('asyncio.sleep', stop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.youtube('@oldname', lambda _: None, lambda _: None,
                                         identity={'id': cid}, on_identity=lambda _: None))
        get.assert_called_once_with(f'https://www.youtube.com/channel/{cid}/live')

    def test_tiktok_and_uncredentialed_twitch_missing_alert_once_per_handle(self):
        from TikTokLive.client.errors import UserNotFoundError
        token = 'testregistrationtoken123'
        for platform in ('tiktok', 'twitch'):
            with self.subTest(platform=platform), mock.patch.object(server, 'save'), mock.patch.object(chat, 'post_discord') as post:
                handle = '@missing' if platform == 'tiktok' else 'missing'
                server.regs[token] = dict(platform=platform, channel=handle, channels={platform: handle}, webhook=HOOK,
                                         identities={platform: {'id': '123', 'handle': handle}})
                checks = []
                async def sleep(_):
                    checks.append(1)
                    self.assertEqual(post.call_count, 0 if len(checks) < 3 else 1)
                    if len(checks) == 6:
                        raise asyncio.CancelledError
                try:
                    if platform == 'tiktok':
                        client = mock.Mock(on=lambda _: lambda fn: fn, connected=False)
                        client.connect = mock.AsyncMock(side_effect=UserNotFoundError('missing', 'fixture'))
                        with mock.patch('TikTokLive.TikTokLiveClient', return_value=client), mock.patch('asyncio.sleep', sleep):
                            with self.assertRaises(asyncio.CancelledError):
                                asyncio.run(chat.tiktok(handle, lambda _: None, lambda s: server.source_status(token, platform, s)))
                    else:
                        reader = mock.Mock(readline=mock.AsyncMock(return_value=b'@room-id=999 :tmi.twitch.tv ROOMSTATE #missing\r\n'))
                        writer = mock.Mock(drain=mock.AsyncMock(), wait_closed=mock.AsyncMock())
                        with mock.patch.dict(chat.os.environ, {'TWITCH_CLIENT_ID': '', 'TWITCH_CLIENT_SECRET': ''}), \
                                mock.patch('asyncio.open_connection', return_value=(reader, writer)), mock.patch('asyncio.sleep', sleep):
                            with self.assertRaises(asyncio.CancelledError):
                                asyncio.run(chat.twitch(handle, lambda _: self.fail('reclaimed handle leaked chat'),
                                                        lambda s: server.source_status(token, platform, s),
                                                        identity={'id': '123'}, on_identity=lambda _: None))
                    self.assertEqual(post.call_count, 1)
                    self.assertEqual(server.channel_status(token, platform), 'Handle not found: did you rename?')
                    server.remember_identity(token, platform, {'id': '123', 'handle': handle})
                    for _ in range(3):
                        server.source_status(token, platform, f'{platform}: not found')
                    self.assertEqual(post.call_count, 1)  # successful ID refresh never resets the sent-alert marker
                finally:
                    server.regs.pop(token)
                    server.stats.pop(token, None)

    def test_twitch_resolves_by_id_not_old_login(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b''
        response.__enter__.return_value.__iter__.return_value = iter(())
        with mock.patch.dict(chat.os.environ, {"TWITCH_CLIENT_ID": "test", "TWITCH_CLIENT_SECRET": "test"}), \
                mock.patch.object(chat, "_twitch_access", return_value="test"), \
                mock.patch.object(chat.json, "load", return_value={"data": [{"id": "123", "login": "renamed"}]}), \
                mock.patch.object(chat.urllib.request, "urlopen", return_value=response) as request:
            self.assertEqual(chat.resolve_identity("twitch", "oldname", {"id": "123"}), {"id": "123", "handle": "renamed"})
        self.assertTrue(request.call_args.args[0].full_url.endswith("users?id=123"))
        with mock.patch.dict(chat.os.environ, {"TWITCH_CLIENT_ID": "", "TWITCH_CLIENT_SECRET": ""}), \
                mock.patch.object(chat.urllib.request, "urlopen") as request:
            self.assertIsNone(chat.resolve_identity("twitch", "oldname", {"id": "123"}))
            request.assert_not_called()

    def test_youtube_resolves_by_id_and_extracts_new_handle(self):
        cid = "UC" + "a" * 22
        text = '"channelMetadataRenderer":' + json.dumps({"externalId": cid, "vanityChannelUrl": "https://www.youtube.com/@renamed"})
        with mock.patch.object(chat, "_yt_get", return_value=text) as get:
            self.assertEqual(chat.resolve_identity("youtube", "@old", {"id": cid}), {"id": cid, "handle": "@renamed"})
        get.assert_called_once_with(f"https://www.youtube.com/channel/{cid}")

    def test_rename_and_missing_alert_are_once_and_saved(self):
        with mock.patch.object(server, "save"), mock.patch.object(chat, "post_discord") as post:
            token = "testregistrationtoken123"
            r = dict(platform="twitch", channel="oldname", channels={"twitch": "oldname"}, webhook=HOOK,
                     identities={"twitch": {"id": "123", "handle": "oldname"}})
            server.regs[token] = r
            try:
                server.remember_identity(token, "twitch", {"id": "123", "handle": "newname"})
                server.remember_identity(token, "twitch", {"id": "123", "handle": "newname"})
                self.assertEqual(r["channel"], "newname")
                self.assertEqual(post.call_count, 1)
                self.assertIn("Noticed you renamed to newname, still connected.", post.call_args.args[1])
                for _ in range(6):
                    server.source_status(token, "twitch", "Twitch: can't find newname")
                self.assertEqual(post.call_count, 2)
                self.assertIn("Handle not found: did you rename?", server.channel_status(token, "twitch"))
                self.assertIn(f"/m/{token}", post.call_args.args[1])
            finally:
                server.regs.pop(token)
                server.stats.pop(token, None)

    def test_best_effort_migration_does_not_stop_startup(self):
        with mock.patch.object(chat, "resolve_identity", side_effect=[TimeoutError, {"id": "123", "handle": "someone"}]), \
                mock.patch.object(server, "save") as save:
            server.regs["migration"] = dict(platform="twitch", channel="someone", channels={"youtube": "@other", "twitch": "someone"})
            try:
                server.resolve_saved_identities("migration")
                self.assertEqual(server.regs["migration"]["identities"]["twitch"]["id"], "123")
                save.assert_called()
            finally:
                server.regs.pop("migration")

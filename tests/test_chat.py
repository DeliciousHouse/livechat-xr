import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import chat  # noqa: E402


class Batch(unittest.TestCase):
    def test_single(self):
        self.assertEqual(chat.batch(["a: 1"]), "a: 1")

    def test_gifts_first(self):
        lines = ["a: 1", "b: 2", "c: 3", "d: 4", "🎁 e sent Rose"]
        self.assertEqual(chat.batch(lines), "🎁 e sent Rose\na: 1\nb: 2\n+2 more")

    def test_overflow(self):
        self.assertEqual(chat.batch(list("abcde")), "a\nb\nc\n+2 more")
        self.assertEqual(chat.batch(list("abcde"), max_lines=5), "a\nb\nc\nd\ne")


class Twitch(unittest.TestCase):
    def line(self, raw):
        return chat.twitch_line(raw, "chan")

    def test_chat_with_display_name(self):
        raw = "@badge-info=;color=#FF0000;display-name=Cool\\sGuy;mod=0 :coolguy!coolguy@coolguy.tmi.twitch.tv PRIVMSG #chan :hello there"
        self.assertEqual(self.line(raw), "Cool Guy: hello there")

    def test_chat_without_tags(self):
        self.assertEqual(self.line(":a!a@a.tmi.twitch.tv PRIVMSG #chan :hi"), "a: hi")

    def test_streamer_chat_skipped(self):
        self.assertIsNone(self.line(":chan!chan@c PRIVMSG #chan :my own message"))

    def test_bits(self):
        raw = "@bits=100;display-name=Fan :fan!fan@f PRIVMSG #chan :Cheer100 great play"
        self.assertEqual(self.line(raw), "🎁 Fan cheered 100 bits: Cheer100 great play")

    def test_sub_uses_system_message(self):
        raw = ("@display-name=Fan;msg-id=resub;system-msg=Fan\\ssubscribed\\sat\\sTier\\s1.\\sThey've\\ssubscribed\\sfor\\s3\\smonths! "
               ":tmi.twitch.tv USERNOTICE #chan :love the stream")
        self.assertEqual(self.line(raw), "🎁 Fan subscribed at Tier 1. They've subscribed for 3 months! — love the stream")

    def test_mass_gift_shown_once(self):
        mystery = "@display-name=Big;msg-id=submysterygift;system-msg=Big\\sis\\sgifting\\s5\\sTier\\s1\\sSubs! :tmi.twitch.tv USERNOTICE #chan"
        part = "@display-name=Big;msg-id=subgift;msg-param-community-gift-id=123;system-msg=x :tmi.twitch.tv USERNOTICE #chan"
        single = "@display-name=Big;msg-id=subgift;system-msg=Big\\sgifted\\sa\\ssub\\sto\\sAmy! :tmi.twitch.tv USERNOTICE #chan"
        self.assertEqual(self.line(mystery), "🎁 Big is gifting 5 Tier 1 Subs!")
        self.assertIsNone(self.line(part))
        self.assertEqual(self.line(single), "🎁 Big gifted a sub to Amy!")

    def test_ignores_non_chat(self):
        for raw in ("PING :tmi.twitch.tv", ":tmi.twitch.tv 001 justinfan1 :Welcome", "@x=y :a!a@a JOIN #chan",
                    "@msg-id=raid;system-msg=x :tmi.twitch.tv USERNOTICE #chan"):
            self.assertIsNone(self.line(raw))

    def test_skips_streamer_and_emits_others(self):
        lines = [b"@display-name=Viewer :viewer!viewer@v PRIVMSG #chan :gg\r\n",
                 b":chan!chan@c PRIVMSG #chan :my own message\r\n", b""]
        reader = mock.Mock(readline=mock.AsyncMock(side_effect=lines))
        writer = mock.Mock(drain=mock.AsyncMock())
        out, sleeps = [], []

        async def stop_after_first(_):
            sleeps.append(1)
            raise asyncio.CancelledError

        with mock.patch("asyncio.open_connection", mock.AsyncMock(return_value=(reader, writer))), \
                mock.patch("asyncio.sleep", stop_after_first):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.twitch("#Chan", out.append, lambda s: None))
        self.assertEqual(out, ["Viewer: gg"])
        self.assertIn(b"JOIN #chan", writer.write.call_args_list[0].args[0])


class TikTok(unittest.TestCase):
    def test_missing_user_backs_off_and_resets_after_offline_or_error(self):
        from TikTokLive.client.errors import UserNotFoundError, UserOfflineError
        missing = UserNotFoundError("me", "missing")
        outcomes = [missing] * 6 + [UserOfflineError("offline"), missing, OSError("outage"), missing]
        client = mock.Mock(on=lambda cls: lambda fn: fn, connected=False)
        client.connect = mock.AsyncMock(side_effect=outcomes)
        sleeps, statuses = [], []

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == len(outcomes):
                raise asyncio.CancelledError

        with mock.patch("TikTokLive.TikTokLiveClient", return_value=client), mock.patch("asyncio.sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.tiktok("@me", lambda s: None, statuses.append))
        self.assertEqual(sleeps, [60, 60, 60, 60, 1800, 1800, 60, 60, 60, 60])
        self.assertIn("Check the spelling or permission to go LIVE", statuses[4])
        self.assertIn("30 minutes", statuses[4])

    def test_gifts_and_comments(self):
        """Fake TikTokLiveClient that replays events through the handlers chat.tiktok registers."""
        from TikTokLive.events import CommentEvent, GiftEvent

        def ev(cls, **kw):
            e = mock.Mock(spec=cls, **kw)
            e.__class__ = cls  # so handler type hints don't matter; dispatch is by registration
            return e

        user = lambda h, n: mock.Mock(unique_id=h, nickname=n)
        gift = mock.Mock()
        gift.name = "Rose"
        events = [
            (CommentEvent, ev(CommentEvent, user=user("viewer", "Viewer"), comment="hi")),
            (CommentEvent, ev(CommentEvent, user=user("Me", "Me"), comment="own message")),
            (GiftEvent, ev(GiftEvent, user=user("fan", "Fan"), gift=gift, streaking=True, repeat_count=3)),
            (GiftEvent, ev(GiftEvent, user=user("fan", "Fan"), gift=gift, streaking=False, repeat_count=5)),
            (GiftEvent, ev(GiftEvent, user=user("one", "One"), gift=gift, streaking=False, repeat_count=1)),
        ]

        class FakeClient:
            def __init__(self, unique_id):
                self.handlers, self.connected = {}, False

            def on(self, cls):
                return lambda fn: self.handlers.setdefault(cls, fn)

            async def connect(self, **_):
                for cls, e in events:
                    if cls in self.handlers:
                        await self.handlers[cls](e)
                raise asyncio.CancelledError

        out = []
        with mock.patch("TikTokLive.TikTokLiveClient", FakeClient):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.tiktok("@me", out.append, lambda s: None))
        self.assertEqual(out, ["Viewer: hi", "🎁 Fan sent Rose x5", "🎁 One sent Rose"])


class Relay(unittest.TestCase):
    def run_relay(self, channels, fake_tiktok, fake_twitch, seconds=0.05, wait=0.2):
        batches, statuses = [], []

        async def go():
            task = asyncio.ensure_future(chat.relay("", "", batches.append, statuses.append, seconds=seconds, channels=channels))
            await asyncio.sleep(wait)
            task.cancel()
            await task

        with mock.patch.object(chat, "tiktok", fake_tiktok), mock.patch.object(chat, "twitch", fake_twitch):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(go())
        return batches, statuses

    def test_both_platforms_share_batches_and_status(self):
        async def fake_tiktok(user, emit, status, key="", ask=None):
            status(f"TikTok: connected to @{user}")
            emit("Fan: hi from tiktok")
            await asyncio.sleep(3600)

        async def fake_twitch(channel, emit, status):
            await asyncio.sleep(0.01)
            status(f"Twitch: connected to #{channel}")
            emit("🎁 Sub cheered 100 bits")
            await asyncio.sleep(3600)

        batches, statuses = self.run_relay({"tiktok": "me", "twitch": "chan"}, fake_tiktok, fake_twitch)
        self.assertEqual(batches, ["🎁 Sub cheered 100 bits\nFan: hi from tiktok"])  # one banner, gift first
        self.assertEqual(statuses, ["TikTok: connected to @me", "TikTok: connected to @me · Twitch: connected to #chan"])

    def test_one_platform_is_unchanged(self):
        async def fake_twitch(channel, emit, status):
            status(f"Twitch: connected to #{channel}")
            emit("a: 1")
            await asyncio.sleep(3600)

        batches, statuses = self.run_relay({"twitch": "c"}, None, fake_twitch)
        self.assertEqual((batches, statuses), (["a: 1"], ["Twitch: connected to #c"]))

    def test_one_source_failing_fails_the_relay(self):
        async def fake_tiktok(user, emit, status, key="", ask=None):
            await asyncio.sleep(3600)

        async def fake_twitch(channel, emit, status):
            raise RuntimeError("boom")

        with mock.patch.object(chat, "tiktok", fake_tiktok), mock.patch.object(chat, "twitch", fake_twitch):
            with self.assertRaises(RuntimeError):
                asyncio.run(chat.relay("", "", lambda t: None, lambda s: None, seconds=0.01, channels={"tiktok": "a", "twitch": "b"}))

    def test_config_channels_and_migration(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)):
            (Path(d) / "config.ini").write_text("[chat]\nplatform = TikTok\nchannel = @old\n", encoding="utf-8")
            cp = chat.load_config()
            self.assertEqual(chat.channels(cp), {"tiktok": "@old"})
            self.assertEqual(cp["chat"]["channel"], "")
            cp["chat"]["twitch"] = "second"
            chat.save_config(cp)
            self.assertEqual(chat.channels(chat.load_config()), {"tiktok": "@old", "twitch": "second"})
            self.assertEqual(chat.load_config()["chat"]["channel"], "")  # the migrated value does not come back


def _yt(kind, name, msg=None, **extra):
    """A get_live_chat chat item the way YouTube sends it: renderer name -> fields with runs/simpleText."""
    r = {"id": extra.pop("id", f"{kind}-{name}"), "authorName": {"simpleText": name}, **extra}
    if msg is not None:
        r["message"] = {"runs": [{"text": msg}]}
    return {kind: r}


class YouTube(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(chat.youtube_line(_yt("liveChatTextMessageRenderer", "Fan", "gg")), "Fan: gg")
        own = _yt("liveChatTextMessageRenderer", "Me", "mine", authorExternalChannelId="UCowner")
        self.assertIsNone(chat.youtube_line(own, "UCowner"))
        self.assertEqual(chat.youtube_line(own, ""), "Me: mine")  # unknown owner: nothing is skipped
        paid = _yt("liveChatPaidMessageRenderer", "Big", "love it", purchaseAmountText={"simpleText": "$5.00"})
        self.assertEqual(chat.youtube_line(paid), "🎁 Big sent $5.00: love it")
        sticker = _yt("liveChatPaidStickerRenderer", "Sti", purchaseAmountText={"simpleText": "€2.00"})
        self.assertEqual(chat.youtube_line(sticker), "🎁 Sti sent €2.00")
        member = _yt("liveChatMembershipItemRenderer", "New", headerSubtext={"simpleText": "Welcome to the club!"})
        self.assertEqual(chat.youtube_line(member), "🎁 New: Welcome to the club!")
        gifted = {"liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": {"id": "g", "header": {"liveChatSponsorshipsHeaderRenderer": {
            "authorName": {"simpleText": "Gen"}, "primaryText": {"runs": [{"text": "Gen gifted 5 memberships"}]}}}}}
        self.assertEqual(chat.youtube_line(gifted), "🎁 Gen gifted 5 memberships")
        self.assertIsNone(chat.youtube_line({"liveChatViewerEngagementMessageRenderer": {"id": "e"}}))

    def test_emoji_runs(self):
        item = {"liveChatTextMessageRenderer": {"id": "1", "authorName": {"simpleText": "A"}, "message": {"runs": [
            {"text": "hi "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}, {"text": " "},
            {"emoji": {"emojiId": "UCx/abc", "isCustomEmoji": True, "shortcuts": [":_hype:"]}}]}}}
        self.assertEqual(chat.youtube_line(item), "A: hi 😂 :_hype:")

    def test_page_live_and_offline(self):
        live = ('x "INNERTUBE_API_KEY":"KEY1" y "INNERTUBE_CLIENT_VERSION":"2.1" "channelId":"UC' + "a" * 22 + '"'
                ' "isLive":true "liveChatRenderer":{"continuations":[{"reloadContinuationData":{"continuation":"CONT1"}}]}')
        self.assertEqual(chat.yt_page(live), {"key": "KEY1", "version": "2.1", "continuation": "CONT1", "owner": "UC" + "a" * 22})
        self.assertIsNone(chat.yt_page('"INNERTUBE_API_KEY":"K" "continuation":"other page"'))  # channel page, not live

    def test_next_and_view_switch(self):
        self.assertEqual(chat.yt_next({"continuations": [{"invalidationContinuationData": {"continuation": "C2", "timeoutMs": 10000}}]}), ("C2", 10))
        self.assertEqual(chat.yt_next({"continuations": [{"timedContinuationData": {"continuation": "C3", "timeoutMs": 100}}]}), ("C3", 1))
        self.assertIsNone(chat.yt_next({"continuations": [{"liveChatReplayContinuationData": {"continuation": "R"}}]}))
        header = {"liveChatHeaderRenderer": {"viewSelector": {"sortFilterSubMenuRenderer": {"subMenuItems": [
            {"title": "Top chat", "selected": True, "continuation": {"reloadContinuationData": {"continuation": "TOP"}}},
            {"title": "Live chat", "selected": False, "continuation": {"reloadContinuationData": {"continuation": "ALL"}}}]}}}}
        self.assertEqual(chat.yt_all_chat({"header": header}), "ALL")
        header["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]["subMenuItems"][1]["selected"] = True
        self.assertIsNone(chat.yt_all_chat({"header": header}))

    def test_backlog_skipped_then_live_emitted(self):
        """Offline -> live: the first responses (Top chat, then the all-chat switch) are history and never shown;
        later responses emit only unseen ids; the replay continuation means the stream ended."""
        nxt = lambda c: {"continuations": [{"invalidationContinuationData": {"continuation": c, "timeoutMs": 1000}}]}
        act = lambda *items: {"actions": [{"addChatItemAction": {"item": i}} for i in items]}
        top_header = {"liveChatHeaderRenderer": {"viewSelector": {"sortFilterSubMenuRenderer": {"subMenuItems": [
            {"title": "Live chat", "selected": False, "continuation": {"reloadContinuationData": {"continuation": "ALL"}}}]}}}}
        old, new1, new2 = (_yt("liveChatTextMessageRenderer", n, n, id=n) for n in ("old", "new1", "new2"))
        responses = {"CONT1": {**act(old), **nxt("X"), "header": top_header},
                     "ALL": {**act(old, _yt("liveChatTextMessageRenderer", "old2", "old2", id="old2")), **nxt("C2")},
                     "C2": {**act(old, new1), **nxt("C3")},
                     "C3": {**act(new1, new2), "continuations": [{"liveChatReplayContinuationData": {"continuation": "R"}}]}}
        pages = iter(['"nothing live here"', 'x "INNERTUBE_API_KEY":"K" "isLive":true "liveChatRenderer":{"continuations":'
                      '[{"reloadContinuationData":{"continuation":"CONT1"}}]}'])
        out, statuses, sleeps = [], [], []

        async def sleep(s):
            sleeps.append(s)
            if len(sleeps) == 4:  # offline wait, two chat waits, then the ended wait
                raise asyncio.CancelledError

        with mock.patch.object(chat, "_yt_get", lambda url: next(pages)), \
                mock.patch.object(chat, "_yt_chat", lambda info, c: responses[c]), mock.patch("asyncio.sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.youtube("@Me", out.append, statuses.append))
        self.assertEqual(out, ["new1: new1", "new2: new2"])
        self.assertEqual(sleeps, [60, 1, 1, 60])
        self.assertEqual(statuses, ["YouTube: waiting for @Me to go live", "YouTube: connected to @Me", "YouTube: @Me went offline, waiting"])

    def test_relay_with_three_platforms_and_config(self):
        async def fake(channel, emit, status):
            status(f"YouTube: connected to @{channel}")
            emit("Yt: hello")
            await asyncio.sleep(3600)

        batches, statuses = [], []

        async def go():
            task = asyncio.ensure_future(chat.relay("", "", batches.append, statuses.append, seconds=0.05, channels={"youtube": "me"}))
            await asyncio.sleep(0.2)
            task.cancel()
            await task

        with mock.patch.object(chat, "youtube", fake):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(go())
        self.assertEqual((batches, statuses), (["Yt: hello"], ["YouTube: connected to @me"]))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)):
            (Path(d) / "config.ini").write_text("[chat]\ntiktok = a\ntwitch = b\nyoutube = @c\n", encoding="utf-8")
            self.assertEqual(chat.channels(chat.load_config()), {"tiktok": "a", "twitch": "b", "youtube": "@c"})


class FollowAsk(unittest.TestCase):
    ASK = "follow for the next sword-only run"

    def test_second_message_once_per_viewer_and_90s_global_cooldown(self):
        s = chat.Session(self.ASK)
        self.assertIsNone(s.comment("a", False, 0))  # first message: no ask
        self.assertEqual(s.comment("a", False, 1), "» " + self.ASK)  # second: ask
        self.assertIsNone(s.comment("a", False, 200))  # once per viewer
        self.assertIsNone(s.comment("b", None, 10))
        self.assertIsNone(s.comment("b", None, 60))  # unknown status is eligible, but 59 s since the last ask
        self.assertEqual(s.comment("b", None, 91), "» " + self.ASK)  # next message after the cooldown
        self.assertEqual(s.asks, 2)

    def test_followers_are_never_asked(self):
        s = chat.Session(self.ASK)
        for t in (0, 100, 200):
            self.assertIsNone(s.comment("fan", True, t))  # known follower
        s.comment("c", False, 0)
        s.follow("c")  # followed during the stream before their second message
        self.assertIsNone(s.comment("c", False, 300))
        s.follow("c")  # re-follow in the same stream counts once
        s.follow("d")
        self.assertEqual((len(s.followers), s.asks), (2, 0))

    def test_empty_text_turns_the_ask_off(self):
        s = chat.Session("  ")
        s.comment("a", False, 0)
        self.assertIsNone(s.comment("a", False, 1))

    def test_following_reads_follow_status_or_unknown(self):
        u = lambda info: mock.Mock(follow_info=info)
        self.assertIs(chat._following(u(None)), None)
        self.assertIs(chat._following(u(mock.Mock(follow_status=0))), False)
        self.assertIs(chat._following(u(mock.Mock(follow_status=1))), True)
        self.assertIs(chat._following(u(mock.Mock(follow_status=2))), True)  # friends
        self.assertIs(chat._following(mock.Mock()), None)  # attribute present but not an int
        self.assertIs(chat._following(None), None)

    def test_ask_line_survives_overflow(self):
        lines = ["a: 1", "b: 2", "c: 3", "d: 4", "» " + self.ASK]
        self.assertEqual(chat.batch(lines), "» " + self.ASK + "\na: 1\nb: 2\n+2 more")

    def test_ask_line_survives_gift_overflow(self):
        s = chat.Session(self.ASK)
        s.comment("a", False, 0)
        ask = s.comment("a", False, 1)
        gifts = [chat.GIFT + name for name in "abc"]
        for max_lines in (1, 3):
            for lines in (gifts + [ask], [gifts[0], ask] + gifts[1:]):
                with self.subTest(max_lines=max_lines, lines=lines):
                    shown = [ask] + gifts[:max_lines - 1]
                    self.assertEqual(chat.batch(lines, max_lines),
                                     "\n".join(shown) + f"\n+{4 - max_lines} more")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)):
            self.assertEqual(s.save("me")["asks_shown"], 1)

    def replay(self, follow_ask):
        """Fake TikTokLiveClient replays a stream through chat.tiktok's handlers, then the stream is cancelled."""
        from TikTokLive.events import CommentEvent, ConnectEvent, FollowEvent, RoomUserSeqEvent

        def ev(cls, **kw):
            e = mock.Mock(spec=cls, **kw)
            e.__class__ = cls
            return e

        user = lambda h, status=0: mock.Mock(unique_id=h, nickname=h.title(), follow_info=mock.Mock(follow_status=status))
        events = [
            (ConnectEvent, ev(ConnectEvent)),
            (RoomUserSeqEvent, ev(RoomUserSeqEvent, total=4)),
            (CommentEvent, ev(CommentEvent, user=user("new"), comment="hi")),
            (CommentEvent, ev(CommentEvent, user=user("fan", 1), comment="gg")),
            (RoomUserSeqEvent, ev(RoomUserSeqEvent, total=9)),
            (CommentEvent, ev(CommentEvent, user=user("fan", 1), comment="nice")),
            (CommentEvent, ev(CommentEvent, user=user("new"), comment="sword only?")),
            (CommentEvent, ev(CommentEvent, user=user("me"), comment="own message")),
            (CommentEvent, ev(CommentEvent, user=user("new"), comment="again")),
            (FollowEvent, ev(FollowEvent, user=user("new"))),
            (RoomUserSeqEvent, ev(RoomUserSeqEvent, total=6)),
        ]

        class FakeClient:
            def __init__(self, unique_id):
                self.handlers, self.connected = {}, False

            def on(self, cls):
                return lambda fn: self.handlers.setdefault(cls, fn)

            async def connect(self, **_):
                for cls, e in events:
                    if cls in self.handlers:
                        await self.handlers[cls](e)
                raise asyncio.CancelledError  # app stopped mid-stream: the summary must still be written

        out = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)), \
                mock.patch("TikTokLive.TikTokLiveClient", FakeClient):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(chat.tiktok("@me", out.append, lambda s: None, "", follow_ask))
            f = Path(d) / "sessions.jsonl"
            rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else None
        return out, rows

    def test_stream_replay_asks_and_writes_session_summary(self):
        out, rows = self.replay(self.ASK)
        self.assertEqual(out, ["New: hi", "Fan: gg", "Fan: nice", "New: sword only?", "» " + self.ASK, "New: again"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({k: row[k] for k in ("channel", "peak_viewers", "new_follows", "asks_shown")},
                         {"channel": "@me", "peak_viewers": 9, "new_follows": 1, "asks_shown": 1})
        self.assertLessEqual(row["start"], row["end"])

    def test_hosted_relay_path_is_unchanged(self):
        out, rows = self.replay(None)  # server.py never passes follow_ask
        self.assertEqual(out, ["New: hi", "Fan: gg", "Fan: nice", "New: sword only?", "New: again"])
        self.assertIsNone(rows)

    def test_relay_and_runner_pass_follow_ask_to_tiktok(self):
        seen = []

        async def fake_tiktok(user, emit, status, key="", ask=None):
            seen.append(ask)
            raise RuntimeError("stop")

        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)):
            cfg = chat.load_config()  # no config.ini: defaults only
        cfg["chat"]["tiktok"], cfg["banner"]["seconds"] = "me", "0.01"
        with mock.patch.object(chat, "tiktok", fake_tiktok), self.assertRaises(RuntimeError):
            asyncio.run(chat.Runner()._main(cfg))
        self.assertEqual(seen, [self.ASK])  # default from DEFAULTS


class Discord(unittest.TestCase):
    def test_post_disables_mentions_and_truncates(self):
        sent = {}

        def fake_urlopen(req, timeout):
            sent["url"], sent["body"], sent["ua"] = req.full_url, req.data, req.get_header("User-agent")
            return mock.Mock(close=lambda: None)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            chat.post_discord("https://discord.com/api/webhooks/1/abc", "@everyone 🎁 Fan sent Rose x5" + "x" * 3000)
        import json
        body = json.loads(sent["body"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(len(body["content"]), 2000)
        self.assertTrue(body["content"].startswith("@everyone 🎁"))
        self.assertIn("LiveChatXR", sent["ua"])

    def test_post_failure_is_swallowed(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("429")):
            chat.post_discord("https://discord.com/api/webhooks/1/abc", "hi")  # must not raise


class Files(unittest.TestCase):
    def test_banner_and_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)):
            chat.write_banner("x: ✔")
            self.assertEqual((Path(d) / "banner.txt").read_text(encoding="utf-8"), "x: ✔")
            cp = chat.load_config()
            self.assertEqual(cp["banner"]["seconds"], "7")
            cp["chat"]["twitch"] = "someone"
            chat.save_config(cp)
            self.assertEqual(chat.load_config()["chat"]["twitch"], "someone")


if __name__ == "__main__":
    unittest.main()

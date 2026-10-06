import asyncio
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
            cp["chat"]["channel"] = "someone"
            chat.save_config(cp)
            self.assertEqual(chat.load_config()["chat"]["channel"], "someone")


if __name__ == "__main__":
    unittest.main()

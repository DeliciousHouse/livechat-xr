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

    def test_overflow(self):
        self.assertEqual(chat.batch(list("abcde")), "a\nb\nc\n+2 more")
        self.assertEqual(chat.batch(list("abcde"), max_lines=5), "a\nb\nc\nd\ne")


class Twitch(unittest.TestCase):
    def test_privmsg_with_display_name(self):
        line = "@badge-info=;color=#FF0000;display-name=Cool\\sGuy;mod=0 :coolguy!coolguy@coolguy.tmi.twitch.tv PRIVMSG #chan :hello there"
        self.assertEqual(chat.parse_twitch(line), ("coolguy", "Cool Guy", "hello there"))

    def test_privmsg_without_tags(self):
        self.assertEqual(chat.parse_twitch(":a!a@a.tmi.twitch.tv PRIVMSG #chan :hi"), ("a", "a", "hi"))

    def test_ignores_non_chat(self):
        for line in ("PING :tmi.twitch.tv", ":tmi.twitch.tv 001 justinfan1 :Welcome", "@x=y :a!a@a JOIN #chan"):
            self.assertIsNone(chat.parse_twitch(line))

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

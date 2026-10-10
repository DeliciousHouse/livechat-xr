import asyncio
import json
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "tools")]
import chat
import qa_relay


class StreamStats(unittest.TestCase):
    def test_tags_and_summaries_share_connection_transitions(self):
        relay = chat.relay
        tag = "\nvia LiveChat XR - livechat.deliciouswines.org"
        for platform in chat.PLATFORMS:
            for plan in ("free", "paid"):
                with self.subTest(platform=platform, plan=plan), qa_relay.fixture():
                    posts, intros = [], []

                    async def go():
                        delivered, finished = asyncio.Queue(), asyncio.Event()

                        async def source(channel, emit, status, *args, tally=None):
                            assert tally is not None
                            status(f"{platform}: connected to qa")
                            tally.comment("one")
                            status(f"{platform}: connected to qa")
                            tally.comment("two")
                            emit("Fan: first")
                            await delivered.get()
                            status(f"{platform}: connected to qa")
                            emit("Fan: second")
                            await delivered.get()
                            status(f"{platform}: qa went offline, waiting")
                            status(f"{platform}: qa went offline, waiting")
                            status(f"{platform}: connected to qa")
                            tally.comment("three")
                            status(f"{platform}: qa went offline, waiting")
                            emit("x" * 2000)
                            await delivered.get()
                            status(f"{platform}: connected to qa")
                            tally.comment("four")
                            status(f"{platform}: retrying (TimeoutError)")
                            status(f"{platform}: qa went offline, waiting")
                            finished.set()
                            await asyncio.Event().wait()

                        async def fast_relay(*args, **kwargs):
                            connect = kwargs["on_connect"]
                            def on_connect():
                                intros.append(platform)
                                connect()
                            kwargs["on_connect"] = on_connect
                            await relay(*args, seconds=.01, **kwargs)

                        def post(url, text):
                            posts.append(text)
                            if not text.startswith("Stream ended"):
                                delivered.put_nowait(None)

                        def thread(target, args, **kwargs):
                            return mock.Mock(start=lambda: target(*args))

                        qa_relay.server.regs["qa"] = dict(platform=platform, channel="qa", plan=plan,
                                                         webhook=qa_relay.HOOKS["fixture-one"])
                        with mock.patch.object(chat, platform, source), mock.patch.object(chat, "relay", fast_relay), \
                                mock.patch.object(chat, "post_discord", side_effect=post), \
                                mock.patch("threading.Thread", side_effect=thread):
                            task = asyncio.create_task(qa_relay.server.run("qa"))
                            try:
                                await asyncio.wait_for(finished.wait(), 2)
                            finally:
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await task

                    asyncio.run(go())
                    rows = [json.loads(line) for line in (qa_relay.server.DATA / "sessions.jsonl").read_text().splitlines()]
                    self.assertEqual([row["comments"] for row in rows], [2, 1])
                    self.assertEqual([row["distinct_chatters"] for row in rows], [2, 1])
                    self.assertEqual(intros, [platform] * 3)
                    self.assertEqual(posts, ["Fan: first" + (tag if plan == "free" else ""), "Fan: second",
                                             chat.session_summary(rows[0]), chat.session_summary(rows[1]),
                                             "x" * (2000 - len(tag)) + tag if plan == "free" else "x" * 2000])

    def test_summary(self):
        row: dict = dict(start="2026-10-10T00:00:00+00:00", end="2026-10-10T01:42:00+00:00",
                   channel="@qa", peak_viewers=9, new_follows=2, asks_shown=0)
        self.assertEqual(chat.session_summary(row),
                         "Stream ended @qa - 1h42m - peak 9 viewers - 2 new follows - 0 follow asks shown")
        row.update(peak_viewers=None, new_follows=None, asks_shown=None)
        self.assertEqual(chat.session_summary(row), "Stream ended @qa - 1h42m")
        row["end"] = row["start"]
        self.assertEqual(chat.session_summary(row), "Stream ended @qa - 0m")

    def test_pc_save_notification_and_no_webhook(self):
        with qa_relay.fixture(), mock.patch.object(chat, "DIR", qa_relay.server.DATA), \
                mock.patch.object(chat, "post_discord") as post, mock.patch.object(chat, "write_banner") as banner:
            s = chat.Session("")
            s.start = datetime.now(timezone.utc)
            row = s.save("qa", qa_relay.HOOKS["fixture-one"])
            text = chat.session_summary(row)
            post.assert_called_once_with(qa_relay.HOOKS["fixture-one"], text)
            banner.assert_called_once_with(text)
            post.reset_mock()
            banner.reset_mock()
            s.save("qa")
            post.assert_not_called()
            banner.assert_not_called()
            banner.side_effect = OSError("fixture banner failure")
            row = s.save("qa", qa_relay.HOOKS["fixture-one"])
            post.assert_called_once_with(qa_relay.HOOKS["fixture-one"], chat.session_summary(row))

    def test_runner_passes_webhook_to_session_source(self):
        seen = []
        async def source(user, emit, status, key="", ask=None, webhook=""):
            seen.append(webhook)
            raise RuntimeError("fixture stop")
        with qa_relay.fixture(), mock.patch.object(chat, "DIR", qa_relay.server.DATA), \
                mock.patch.object(chat, "tiktok", source):
            cfg = chat.load_config()
            cfg["chat"]["tiktok"] = "qa"
            cfg["chat"]["discord_webhook"] = qa_relay.HOOKS["fixture-one"]
            cfg["banner"]["seconds"] = ".01"
            with self.assertRaisesRegex(RuntimeError, "fixture stop"):
                asyncio.run(chat.Runner()._main(cfg))
        self.assertEqual(seen, [qa_relay.HOOKS["fixture-one"]])

    def test_server_stats_write_and_post_failures_are_isolated(self):
        async def relay(platform, channel, on_batch, status, **kwargs):
            row = chat.Tally(platform, channel).row()
            kwargs["on_session"](row)
            on_batch("One: chat continues")
            raise asyncio.CancelledError

        def thread(target, args, **kwargs):
            return mock.Mock(start=lambda: target(*args))

        with qa_relay.fixture(), mock.patch.object(chat, "relay", relay), \
                mock.patch.object(chat, "post_discord", side_effect=[OSError("fixture post failure"), True]) as post, \
                mock.patch("threading.Thread", side_effect=thread), \
                mock.patch("builtins.open", side_effect=OSError("fixture write failure")), \
                self.assertLogs("relay", level="WARNING") as logs:
            qa_relay.server.regs["fixture-registration"] = dict(platform="tiktok", channel="@qa",
                                                              webhook=qa_relay.HOOKS["fixture-one"])
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(qa_relay.server.run("fixture-registration"))
            self.assertEqual(post.call_args_list[-1].args[1], "One: chat continues")
            self.assertEqual(post.call_count, 2)
            self.assertIn("stream stats persistence failed", "\n".join(logs.output))
            self.assertIn("stream stats notification failed", "\n".join(logs.output))

    def test_relay_persists_and_posts_once_per_offline_connection(self):
        posted = threading.Event()
        async def fake_tiktok(user, emit, status, key="", ask=None, tally=None):
            assert tally is not None
            status("TikTok: connected to @qa")
            tally.comment("one")
            tally.comment("one")
            tally.comment("two")
            tally.gifts += 5
            tally.followers.update(["one", "one"])
            tally.peak = 9
            emit("One: hello")
            status("TikTok: @qa went offline, waiting")
            status("TikTok: @qa went offline, waiting")
            await asyncio.Event().wait()

        async def go():
            task = asyncio.create_task(qa_relay.server.run("fixture-registration"))
            try:
                self.assertTrue(await asyncio.to_thread(posted.wait, 2))
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with qa_relay.fixture(), mock.patch.object(chat, "tiktok", fake_tiktok), \
                mock.patch.object(chat, "post_discord", side_effect=lambda *_: posted.set() or True) as post:
            qa_relay.server.regs["fixture-registration"] = dict(platform="tiktok", channel="@qa",
                                                              webhook=qa_relay.HOOKS["fixture-one"])
            asyncio.run(go())
            rows = [json.loads(line) for line in (qa_relay.server.DATA / "sessions.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual([row[k] for k in ("registration_id", "platform", "comments", "distinct_chatters",
                                              "gifts", "peak_viewers", "new_follows")],
                             ["fixture-registration", "tiktok", 3, 2, 5, 9, 1])
            post.assert_called_once_with(qa_relay.HOOKS["fixture-one"], chat.session_summary(row))

    def test_reconnect_reset_and_callback_failure_do_not_stop_chat(self):
        rows, batches = [], []
        connect = mock.Mock(side_effect=OSError("fixture intro failure"))

        async def source(user, emit, status, key="", ask=None, tally=None):
            assert tally is not None
            for _ in range(2):
                status("TikTok: connected to @qa")
                tally.comment("one")
                emit("One: hello")
                status("TikTok: @qa went offline, waiting")
            status("TikTok: connected to @qa")
            tally.comment("two")
            status("TikTok: retrying (TimeoutError)")
            status("TikTok: @qa went offline, waiting")
            await asyncio.Event().wait()

        def fail(row):
            rows.append(row)
            raise OSError("fixture stats failure")

        async def go():
            received = asyncio.Event()
            def batch(text):
                batches.append(text)
                received.set()
            task = asyncio.create_task(chat.relay("tiktok", "qa", batch, lambda _: None,
                                                   seconds=.01, on_session=fail, on_connect=connect))
            try:
                await asyncio.wait_for(received.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with qa_relay.fixture(), mock.patch.object(chat, "tiktok", source):
            asyncio.run(go())
        self.assertEqual([row["comments"] for row in rows], [1, 1])
        self.assertEqual(batches, ["One: hello\nOne: hello"])
        self.assertEqual(connect.call_count, 3)


if __name__ == "__main__":
    unittest.main()

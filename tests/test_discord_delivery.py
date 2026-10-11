"""Lossless Discord delivery; synthetic chat and HTTP only."""
import asyncio
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from email.message import Message
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import chat


class Delivery(unittest.IsolatedAsyncioTestCase):
    async def test_pc_runner_keeps_all_four_discord_lines(self):
        sent, banners = [], []
        delivered = asyncio.Event()
        loop = asyncio.get_running_loop()
        async def source(channel, emit, status):
            for i in range(4):
                emit(f"Fan: {i}")
            await asyncio.Event().wait()
        def post(url, text, **kwargs):
            sent.append(text)
            loop.call_soon_threadsafe(delivered.set)
            return True
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat, "DIR", Path(d)), \
                mock.patch.object(chat, "twitch", source), mock.patch.object(chat, "post_discord", post), \
                mock.patch.object(chat, "write_banner", side_effect=banners.append):
            cfg = chat.load_config()
            cfg["chat"].update(twitch="qa", discord_webhook="fixture", show_tag="false")
            cfg["banner"]["seconds"] = ".01"
            task = asyncio.create_task(chat.Runner()._main(cfg))
            try:
                await asyncio.wait_for(delivered.wait(), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sent, ["Fan: 0\nFan: 1\nFan: 2\nFan: 3"])
        self.assertEqual(banners, ["Fan: 0\nFan: 1\nFan: 2\n+1 more"])

    async def test_hosted_mixed_burst_records_all_ten_lines(self):
        from tests.test_account import server, HOOK
        sent, emitted = [], []
        real_relay = chat.relay
        async def source(channel, emit, status, *args, **kwargs):
            for i in range(5):
                line = f"{channel}: {i}"
                emitted.append(line)
                emit(line)
            await asyncio.Event().wait()
        async def fast_relay(*args, **kwargs):
            await real_relay(*args, seconds=.01, **kwargs)
        async def complete():
            while server.stats.get("fixture", {}).get("lines_posted", 0) < 10:
                await asyncio.sleep(.001)
        with mock.patch.object(server, "regs", {"fixture": dict(platform="tiktok", channel="tk", plan="paid",
                webhook=HOOK, channels={"tiktok": "tk", "twitch": "tw"})}), \
                mock.patch.object(server, "stats", {}), mock.patch.object(server, "status", {}), \
                mock.patch.object(chat, "tiktok", source), mock.patch.object(chat, "twitch", source), \
                mock.patch.object(chat, "relay", fast_relay), \
                mock.patch.object(chat, "post_discord", side_effect=lambda _, text, **kw: sent.append(text) or True):
            task = asyncio.create_task(server.run("fixture"))
            try:
                await asyncio.wait_for(complete(), 2)
                st = server.stats["fixture"]
                self.assertEqual([st[k] for k in ("lines_received", "lines_posted", "lines_dropped")], [10, 10, 0])
                self.assertEqual(server.delivery_status("fixture"), "All 10 chat messages delivered.")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual("\n".join(sent).splitlines(), emitted)
        self.assertEqual(len(sent), 2)

    async def test_bursts_and_banner_are_independent(self):
        for counts in ((4, 0), (5, 5)):
            with self.subTest(counts=counts):
                posted, banners, events, emitted = [], [], [], []
                done = asyncio.Event()
                loop = asyncio.get_running_loop()
                def post(url, text, **kwargs):
                    posted.append(text)
                    if len("\n".join(posted).splitlines()) == sum(counts):
                        loop.call_soon_threadsafe(done.set)
                    return True
                async def source(channel, emit, status, *args, **kwargs):
                    n = counts[0 if channel == "tk" else 1]
                    for i in range(n):
                        line = f"{channel}: {i}"
                        emitted.append(line)
                        emit(line)
                    await asyncio.Event().wait()
                sender = chat.DiscordQueue("fixture", on_delivery=lambda **kw: events.append(kw))
                with mock.patch.object(chat, "post_discord", post), \
                        mock.patch.object(chat, "tiktok", source), mock.patch.object(chat, "twitch", source):
                    worker = asyncio.create_task(sender.run())
                    relay = asyncio.create_task(chat.relay("", "", banners.append, lambda _: None,
                        seconds=0.01, channels={"tiktok": "tk", "twitch": "tw"},
                        on_lines=lambda lines, platforms: sender.put(lines, platforms)))
                    try:
                        await asyncio.wait_for(done.wait(), 2)
                        await asyncio.wait_for(sender.join(), 2)
                    finally:
                        relay.cancel()
                        worker.cancel()
                        await asyncio.gather(relay, worker, return_exceptions=True)
                self.assertEqual("\n".join(posted).splitlines(), emitted)
                self.assertTrue(all(len(p.splitlines()) <= 5 and len(p) <= 2000 for p in posted))
                self.assertEqual(banners, [chat.batch(emitted)])
                self.assertEqual(sum(e.get("posted", 0) for e in events), sum(counts))
                self.assertEqual(sum(e.get("received", 0) for e in events), sum(counts))

    async def test_429_retries_identical_payload_without_loss(self):
        headers = Message()
        headers["Retry-After"] = "0.01"
        responses = [urllib.error.HTTPError("fixture", 429, "limited", headers,
                                           io.BytesIO(b'{"retry_after":0.01}')), mock.Mock()]
        sender = chat.DiscordQueue("https://discord.com/api/webhooks/1/fixture")
        sender.put(["Fan: one", "Fan: two"])
        with mock.patch.object(chat.urllib.request, "urlopen", side_effect=responses) as request:
            worker = asyncio.create_task(sender.run())
            try:
                await asyncio.wait_for(sender.join(), 2)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[0].data, request.call_args_list[1].args[0].data)
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["content"], "Fan: one\nFan: two")
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_429_json_retry_after_without_header(self):
        error = urllib.error.HTTPError("fixture", 429, "limited", Message(), io.BytesIO(b'{"retry_after":0.02}'))
        retry, errors = [], []
        with mock.patch.object(chat.urllib.request, "urlopen", side_effect=error):
            self.assertFalse(chat.post_discord("https://discord.com/api/webhooks/1/fixture", "Fan: one",
                                              on_retry=retry.append, on_error=errors.append))
        self.assertEqual(retry, [.02])
        self.assertEqual(errors, [429])

    async def test_long_lines_and_tag_never_truncate(self):
        sent, events = [], []
        sender = chat.DiscordQueue("fixture", on_delivery=lambda **kw: events.append(kw))
        lines = ["Fan: " + "😀" * 2500, "Other: hi"]
        sender.put(lines, ["twitch"], suffix="via LiveChat XR")
        with mock.patch.object(chat, "post_discord", side_effect=lambda _, text, **kw: sent.append(text) or True):
            worker = asyncio.create_task(sender.run())
            try:
                await asyncio.wait_for(sender.join(), 2)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual("".join(sent).replace("\n", ""), "".join(lines) + "via LiveChat XR")
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= 2000 for text in sent))
        self.assertEqual(sum(e.get("posted", 0) for e in events), 2)

    async def test_rate_limit_and_old_backlog_are_counted(self):
        now, sent, events = [0.0], [], []
        async def sleep(delay):
            now[0] += delay
        sender = chat.DiscordQueue("fixture", on_delivery=lambda **kw: events.append(kw))
        with mock.patch.object(chat, "time", mock.Mock(monotonic=lambda: now[0])), \
                mock.patch.object(chat.asyncio, "sleep", sleep), \
                mock.patch.object(chat, "post_discord", side_effect=lambda _, text, **kw: sent.append((now[0], text)) or True):
            sender.put([f"Fan: {i}" for i in range(130)])
            now[0] = 59
            sender.put(["Fan: fresh"])
            worker = asyncio.create_task(sender.run())
            try:
                await sender.join()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(len(sent), 27)
        self.assertGreaterEqual(sent[25][0] - sent[0][0], 60)
        self.assertEqual(sent[-2][1], "+5 earlier messages")
        self.assertEqual(sent[-1][1], "Fan: fresh")
        self.assertEqual(sum(e.get("dropped_backlog", 0) for e in events), 5)
        self.assertEqual(sum(e.get("posted", 0) for e in events), 126)


if __name__ == "__main__":
    unittest.main()

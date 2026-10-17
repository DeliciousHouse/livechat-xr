"""Lossless Discord delivery; synthetic chat and HTTP only."""
import asyncio
import io
import json
import sys
import tempfile
import threading
import time
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
        self.assertEqual(sent, ["🟣 Fan: 0\n🟣 Fan: 1\n🟣 Fan: 2\n🟣 Fan: 3"])
        self.assertEqual(banners, ["🟣 Fan: 0\n🟣 Fan: 1\n🟣 Fan: 2\n+1 more"])

    async def test_hosted_mixed_burst_records_all_ten_lines(self):
        await self.hosted_with_collector("baseline")

    async def test_hosted_blocking_collector_keeps_each_line_under_half_second(self):
        await self.hosted_with_collector("blocked")

    async def test_hosted_crashing_collector_delivers_ten_within_two_seconds(self):
        await self.hosted_with_collector("crashing")

    async def test_hosted_missing_each_secret_delivers_ten_within_two_seconds(self):
        from tests.test_network import network
        for name in network.REQUIRED:
            with self.subTest(missing=name):
                await self.hosted_with_collector(name)

    async def hosted_with_collector(self, mode):
        from tests.test_account import server, HOOK
        from tests.test_network import network, config
        from nacl.signing import SigningKey
        sent, emitted, emit_times, post_times = [], [], {}, {}
        ready, entered, collector_done = threading.Event(), threading.Event(), threading.Event()
        emitters = {}
        loop = asyncio.get_running_loop()
        collector_times = []
        def collect():
            collector_times.append(time.monotonic())
            entered.set()
            if mode == "blocked":
                time.sleep(5)
                collector_done.set()
            if mode == "crashing":
                raise RuntimeError("synthetic collector failure")
            return []
        def produce():
            if not ready.wait(3) or not entered.wait(3): return
            for channel in ("tk", "tw"):
                for i in range(5):
                    line = f"{channel}: {i}"
                    tagged = chat.ICONS["tiktok" if channel == "tk" else "twitch"] + line
                    emitted.append(tagged)
                    emit_times[tagged] = time.monotonic()
                    loop.call_soon_threadsafe(emitters[channel], line)
                    if mode != "baseline": time.sleep(.01)
        def post(_, text, **kw):
            sent.append(text)
            for line in text.splitlines():
                post_times[line] = time.monotonic()
            return True
        real_relay = chat.relay
        async def source(channel, emit, status, *args, **kwargs):
            emitters[channel] = emit
            if len(emitters) == 2: ready.set()
            await asyncio.Event().wait()
        async def fast_relay(*args, **kwargs):
            await real_relay(*args, seconds=.01, **kwargs)
        async def complete():
            while server.stats.get("fixture", {}).get("lines_posted", 0) < 10:
                await asyncio.sleep(.001)
        cfg = config()
        cfg["DISCORD_PUBLIC_KEY"] = SigningKey.generate().verify_key.encode().hex()
        if mode in network.REQUIRED: cfg.pop(mode)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(server, "regs", {"fixture": dict(platform="tiktok", channel="tk", plan="paid",
                webhook=HOOK, channels={"tiktok": "tk", "twitch": "tw"})}), \
                mock.patch.object(server, "stats", {}), mock.patch.object(server, "status", {}), \
                mock.patch.object(chat, "tiktok", source), mock.patch.object(chat, "twitch", source), \
                mock.patch.object(chat, "relay", fast_relay), \
                mock.patch.object(chat, "post_discord", post):
            net = network.Network(Path(tmp), server.normalise_channel, cfg)
            task = asyncio.create_task(server.run("fixture"))
            producer = threading.Thread(target=produce)
            try:
                with mock.patch.object(net, "collect_twitch", collect), \
                        mock.patch.object(net, "collect_youtube", return_value=[]), \
                        mock.patch.object(net, "register_commands", new=mock.AsyncMock()):
                    await net.start()
                    if not net.enabled: entered.set()
                    producer.start()
                    await asyncio.wait_for(complete(), 2)
                producer.join(1)
                st = server.stats["fixture"]
                self.assertEqual([st[k] for k in ("lines_received", "lines_posted", "lines_dropped")], [10, 10, 0])
                self.assertEqual(server.delivery_status("fixture"), "All 10 chat messages delivered.")
                self.assertEqual(len(post_times), 10)
                if mode == "blocked":
                    self.assertFalse(collector_done.is_set(), "chat must finish while collector is still blocked")
                    self.assertTrue(all(post_times[k] - t < .5 for k, t in emit_times.items()))
                self.assertLess(max(post_times.values()) - min(emit_times.values()), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await net.stop()
                if producer.ident: producer.join(1)
                if mode == "blocked":
                    self.assertTrue(await asyncio.to_thread(collector_done.wait, 6))
                    self.assertGreaterEqual(time.monotonic() - collector_times[0], 5)
        self.assertEqual("\n".join(sent).splitlines(), emitted)
        if mode == "baseline": self.assertEqual(len(sent), 2)

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

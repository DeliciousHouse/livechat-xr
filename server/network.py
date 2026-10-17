"""Private Pop1 discovery, isolated from hosted chat on the relay's asyncio loop."""
import asyncio
import contextlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

import chat

log = logging.getLogger("network")
REQUIRED = ("DISCORD_BOT_TOKEN", "DISCORD_PUBLIC_KEY", "DISCORD_CLIENT_ID", "TWITCH_CLIENT_ID",
            "TWITCH_CLIENT_SECRET", "YOUTUBE_API_KEY", "POP1_LIVE_GUILD_ID", "POP1_LIVE_CHANNEL_ID",
            "POP1_LIVE_FOUNDER_ID")
INTERVALS = {"twitch": 300, "youtube": 900}
GAME = "Population: ONE"
COMMANDS = [{"name": "network-status", "description": "Check private Pop1 discovery status", "type": 1}]


def message(text, *, viewer_role=None):
    """Escape external text; only the separately supplied role may generate a mention."""
    text = re.sub(r"([\\`*_{}\[\]()<>~|#])", r"\\\1", str(text)).replace("@", "@\u200b")
    allowed = {"parse": []}
    if viewer_role is not None:
        if not re.fullmatch(r"\d{1,22}", viewer_role):
            raise ValueError("invalid viewer role")
        allowed["roles"] = [viewer_role]
        text += " <@&" + viewer_role + ">"
    return {"content": text, "allowed_mentions": allowed, "flags": 4}


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY, discord_id TEXT NOT NULL DEFAULT '',
                    opted_in INTEGER NOT NULL DEFAULT 0 CHECK(opted_in IN (0,1)),
                    review_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(review_status IN ('pending','reviewed','removed')),
                    dm_closed INTEGER NOT NULL DEFAULT 0 CHECK(dm_closed IN (0,1)));
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
                    platform TEXT NOT NULL CHECK(platform IN ('twitch','youtube')),
                    platform_id TEXT NOT NULL, handle TEXT NOT NULL,
                    UNIQUE(platform, platform_id));
                CREATE TABLE IF NOT EXISTS presence (
                    channel_id INTEGER PRIMARY KEY REFERENCES channels(id),
                    stream_id TEXT NOT NULL, title TEXT NOT NULL, viewers INTEGER,
                    game TEXT NOT NULL, seen_at REAL NOT NULL, live INTEGER NOT NULL);
            """)

    def upsert_presence(self, platform, rows, now):
        # A complete successful poll replaces this source's presence, never its consent/review.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE presence SET live=0 WHERE channel_id IN (SELECT id FROM channels WHERE platform=?)", (platform,))
            for row in rows:
                channel = db.execute("SELECT id FROM channels WHERE platform=? AND platform_id=?",
                                     (platform, row["platform_id"])).fetchone()
                if channel is None:
                    lid = db.execute("INSERT INTO listings DEFAULT VALUES").lastrowid
                    cid = db.execute("INSERT INTO channels(listing_id,platform,platform_id,handle) VALUES (?,?,?,?)",
                                     (lid, platform, row["platform_id"], row["handle"])).lastrowid
                else:
                    cid = channel["id"]
                    if row["handle"] != row["platform_id"]:
                        db.execute("UPDATE channels SET handle=? WHERE id=?", (row["handle"], cid))
                db.execute("""INSERT INTO presence(channel_id,stream_id,title,viewers,game,seen_at,live)
                    VALUES (?,?,?,?,?,?,1)
                    ON CONFLICT(channel_id) DO UPDATE SET stream_id=excluded.stream_id, title=excluded.title,
                    viewers=excluded.viewers, game=excluded.game, seen_at=excluded.seen_at, live=1""",
                    (cid, row["stream_id"], row["title"], row["viewers"], row["game"], now))

    def set_listing(self, listing_id, **fields):
        if not fields or not fields.keys() <= {"opted_in", "review_status", "discord_id", "dm_closed"}:
            raise ValueError("invalid listing fields")
        with self.connect() as db:
            db.execute("UPDATE listings SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                       (*fields.values(), listing_id))

    def listing(self, listing_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            return dict(row) if row else None

    def channels(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM channels ORDER BY id")]

    def discovery(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT c.*, p.title,p.viewers,p.stream_id,p.seen_at
                FROM channels c JOIN listings l ON l.id=c.listing_id JOIN presence p ON p.channel_id=c.id
                WHERE l.opted_in=1 AND l.review_status='reviewed' AND p.live=1
                AND p.game=? AND p.viewers BETWEEN 0 AND 10 AND p.seen_at>?
                ORDER BY p.viewers,c.id""", (GAME, now - 1800))]

    def dm_closed_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM listings WHERE dm_closed=1").fetchone()[0]


class HTTPFailure(Exception):
    def __init__(self, status, retry_after=0, quota=False):
        self.status, self.retry_after, self.quota = status, retry_after, quota
        super().__init__(f"HTTP {status}" + (" quota" if quota else ""))


def retry_delay(value):
    try:
        delay = float(value)
        return max(0, delay) if math.isfinite(delay) else 60
    except (ValueError, TypeError):
        return 60


async def wait_retry(delay):
    # Windows event-loop timers can wake early; never retry before Discord's deadline.
    deadline = time.perf_counter() + delay
    while (remaining := deadline - time.perf_counter()) > 0:
        await asyncio.sleep(remaining)


def request_json(url, *, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except (ValueError, TypeError):
            body = {}
        if isinstance(body, dict) and e.code == 429 and "retry_after" not in body:
            if e.headers.get("Retry-After"):
                body["retry_after"] = retry_delay(e.headers["Retry-After"])
            elif e.headers.get("Ratelimit-Reset"):
                body["retry_after"] = max(0, retry_delay(e.headers["Ratelimit-Reset"]) - time.time())
        return e.code, body


class Network:
    def __init__(self, data, normalise_channel, config=None):
        self.config = dict(os.environ if config is None else config)
        self.normalise_channel = normalise_channel
        self.store = Store(Path(data) / "listings.db")
        self.enabled = False
        self.missing = [k for k in REQUIRED if not self.config.get(k)]
        self.error = ""
        self.loop = None
        self.tasks = set()
        self.io_tasks = set()
        self.stopping = threading.Event()
        self.guard = threading.Lock()
        self.sources = {p: {"last_success": 0, "error": "", "success_streak": 0} for p in INTERVALS}
        self.seen_interactions = {}

    def snapshot(self):
        with self.guard:
            return {"enabled": self.enabled, "missing": list(self.missing), "error": self.error,
                    "sources": {p: dict(s) for p, s in self.sources.items()}}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def offload(self, function, *args, **kwargs):
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.io_tasks.add(worker)
        def finished(task):
            self.io_tasks.discard(task)
            if not task.cancelled(): task.exception()
        worker.add_done_callback(finished)
        # Cancelling a coroutine cannot stop its thread; keep the worker joinable at shutdown.
        return await asyncio.shield(worker)

    async def start(self):
        if self.enabled:
            return
        if self.missing:
            log.error("Discovery off: missing %s", ", ".join(self.missing))
            return
        self.stopping.clear()
        try:
            VerifyKey(bytes.fromhex(self.config["DISCORD_PUBLIC_KEY"]))
            await self.offload(self.store.initialize)
        except Exception as exc:
            self.error = type(exc).__name__
            log.error("Discovery off: %s", self.error)
            return
        if self.stopping.is_set():
            return
        self.loop = asyncio.get_running_loop()
        self.enabled = True
        for platform in INTERVALS:
            self.spawn(self.poll(platform))
        self.spawn(self.register_commands())

    async def stop(self):
        self.enabled = False
        self.stopping.set()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*list(self.io_tasks), return_exceptions=True)
        self.tasks.clear()

    async def poll(self, platform):
        failures = 0
        while not self.stopping.is_set():
            delay = INTERVALS[platform]
            try:
                rows = await self.offload(getattr(self, "collect_" + platform))
                if self.stopping.is_set():
                    return
                now = time.time()
                await self.offload(self.store.upsert_presence, platform, rows, now)
                with self.guard:
                    state = self.sources[platform]
                    state.update(last_success=now, error="", success_streak=state["success_streak"] + 1)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                reason = str(exc) if isinstance(exc, HTTPFailure) else type(exc).__name__
                with self.guard:
                    self.sources[platform].update(error=reason, success_streak=0)
                log.warning("%s collector: %s", platform, reason)
                delay = max(delay, min(3600, 30 * 2 ** min(failures, 7)))
                if isinstance(exc, HTTPFailure):
                    delay = max(delay, exc.retry_after, 3600 if exc.quota else 0)
            await asyncio.sleep(delay)

    def api(self, url, params, headers=None):
        if self.stopping.is_set():
            raise RuntimeError("stopping")
        code, body = request_json(url + "?" + urllib.parse.urlencode(params), headers=headers)
        if code != 200:
            quota = any(e.get("reason") in ("quotaExceeded", "dailyLimitExceeded")
                        for e in body.get("error", {}).get("errors", []))
            raise HTTPFailure(code, retry_delay(body.get("retry_after", 0)), quota)
        return body

    def collect_twitch(self):
        # Adapted from census/twitch.py helix: same category + cursor traversal, no census DB/scheduler.
        try:
            token = chat._twitch_access()
        except urllib.error.HTTPError as exc:
            raise HTTPFailure(exc.code, retry_delay(exc.headers.get("Retry-After", 0))) from None
        headers = {"Client-ID": self.config["TWITCH_CLIENT_ID"], "Authorization": "Bearer " + token}
        base = "https://api.twitch.tv/helix/"
        games = self.api(base + "games", {"name": GAME}, headers)["data"]
        game_id = next(g["id"] for g in games if g["name"].casefold() == GAME.casefold())
        rows, cursor, seen = {}, "", set()
        while True:
            params = {"game_id": game_id, "first": 100}
            if cursor: params["after"] = cursor
            body = self.api(base + "streams", params, headers)
            for s in body["data"]:
                if s["game_id"] != game_id: continue
                rows[s["user_id"]] = dict(platform_id=s["user_id"], handle=s["user_login"].lower(),
                    stream_id=s["id"], title=s["title"], viewers=int(s["viewer_count"]), game=GAME)
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor: return list(rows.values())
            if cursor in seen: raise ValueError("repeated cursor")
            seen.add(cursor)

    def collect_youtube(self):
        # census/youtube.py api: one Pop1 query, paginated, official live detail verification; no scrape fallback.
        base = "https://www.googleapis.com/youtube/v3/"
        rows, cursor, seen = {}, "", set()
        while True:
            params = dict(part="snippet", eventType="live", type="video", q='"Population: ONE"',
                          maxResults=50, key=self.config["YOUTUBE_API_KEY"])
            if cursor: params["pageToken"] = cursor
            body = self.api(base + "search", params)
            ids = sorted({s["id"]["videoId"] for s in body["items"]})
            if ids:
                details = self.api(base + "videos", dict(part="liveStreamingDetails,snippet", id=",".join(ids),
                                                         key=self.config["YOUTUBE_API_KEY"]))
                for s in details["items"]:
                    live, snippet = s.get("liveStreamingDetails", {}), s["snippet"]
                    if not live.get("actualStartTime") or live.get("actualEndTime"): continue
                    text = snippet["title"] + " " + snippet.get("description", "")
                    if not re.search(r"(?<!\w)population\s*:?\s*one(?!\w)", text, re.I): continue
                    viewers = int(live["concurrentViewers"]) if "concurrentViewers" in live else None
                    cid = snippet["channelId"]
                    # A channel can have multiple broadcasts; use the largest, never understate its audience.
                    old = rows.get(cid)
                    if old and (old["viewers"] is None or (viewers is not None and old["viewers"] >= viewers)): continue
                    rows[cid] = dict(platform_id=cid, handle=cid, stream_id=s["id"], title=snippet["title"],
                                     viewers=viewers, game=GAME)
            cursor = body.get("nextPageToken")
            if not cursor: return list(rows.values())
            if cursor in seen: raise ValueError("repeated cursor")
            seen.add(cursor)

    async def resolve_identity(self, platform, channel):
        handle = self.normalise_channel(platform, channel)
        if platform == "twitch":
            return await self.offload(chat.resolve_identity, platform, handle)
        if platform == "youtube":
            # The shared resolver scrapes with a 15s timeout, so network uses only the official API.
            params = dict(part="snippet", key=self.config["YOUTUBE_API_KEY"])
            params["id" if re.fullmatch(r"UC[\w-]{22}", handle) else "forHandle"] = handle
            body = await self.offload(self.api, "https://www.googleapis.com/youtube/v3/channels", params)
            items = body["items"]
            return {"id": items[0]["id"], "handle": items[0]["snippet"].get("customUrl", handle)} if items else None
        return None  # O6 unresolved: no TikTok presence or ownership claims here.

    def _discord(self, method, path, payload):
        headers = {"Content-Type": "application/json"}
        if not path.startswith("/webhooks/"):
            headers["Authorization"] = "Bot " + self.config["DISCORD_BOT_TOKEN"]
        return request_json("https://discord.com/api/v10" + path, headers=headers, data=payload, method=method)

    async def discord(self, method, path, payload):
        for attempt in range(4):
            code, body = await self.offload(self._discord, method, path, payload)
            if code == 429 and attempt < 3:
                await wait_retry(retry_delay(body.get("retry_after", 60)))
                continue
            if isinstance(body, dict) and body.get("code") == 50007:
                return {"dm_closed": True}
            if not 200 <= code < 300:
                raise HTTPFailure(code)
            return body

    async def register_commands(self):
        while not self.stopping.is_set():
            try:
                await self.discord("PUT", f'/applications/{self.config["DISCORD_CLIENT_ID"]}/guilds/{self.config["POP1_LIVE_GUILD_ID"]}/commands', COMMANDS)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Discord command registration: %s", type(exc).__name__)
                await asyncio.sleep(300)

    async def send_dm(self, listing_id, text):
        listing = await self.offload(self.store.listing, listing_id)
        if not listing or listing["dm_closed"] or not listing["discord_id"]: return False
        channel = await self.discord("POST", "/users/@me/channels", {"recipient_id": listing["discord_id"]})
        if not channel.get("dm_closed"):
            channel = await self.discord("POST", f'/channels/{channel["id"]}/messages', message("Pop1 Live: " + text))
        if channel.get("dm_closed"):
            await self.offload(self.store.set_listing, listing_id, dm_closed=True)
            return False
        return True

    def interaction(self, headers, raw):
        try:
            timestamp = headers["X-Signature-Timestamp"]
            if abs(time.time() - int(timestamp)) > 300: return 401, {}
            VerifyKey(bytes.fromhex(self.config.get("DISCORD_PUBLIC_KEY", ""))).verify(
                timestamp.encode() + raw, bytes.fromhex(headers["X-Signature-Ed25519"]))
        except (ValueError, TypeError, KeyError, BadSignatureError):
            return 401, {}
        try:
            body = json.loads(raw)
            if not isinstance(body, dict): return 400, {}
        except (ValueError, UnicodeError):
            return 400, {}
        if body.get("type") == 1: return 200, {"type": 1}
        if not self.enabled or self.loop is None: return 503, {}
        try:
            user = body.get("member", {}).get("user", {}) if body.get("guild_id") else body.get("user", {})
            if body.get("guild_id"):
                if body["guild_id"] != self.config["POP1_LIVE_GUILD_ID"]: return 403, {}
            elif user.get("id") != self.config["POP1_LIVE_FOUNDER_ID"]:
                return 403, {}
            if body.get("application_id") != self.config["DISCORD_CLIENT_ID"]: return 403, {}
            if body.get("type") != 2 or body.get("data", {}).get("name") not in {c["name"] for c in COMMANDS}:
                return 200, {"type": 4, "data": dict(message("This command is not available in the private test."), flags=64)}
            if not all(isinstance(body.get(k), str) and re.fullmatch(r"[\w.-]{1,256}", body[k]) for k in ("id", "token")):
                return 400, {}
        except (AttributeError, TypeError):
            return 400, {}
        with self.guard:
            now = time.time()
            self.seen_interactions = {k: t for k, t in self.seen_interactions.items() if t > now - 300}
            if body["id"] not in self.seen_interactions:
                if len(self.seen_interactions) >= 1000: return 429, {}
                self.seen_interactions[body["id"]] = now
                self.loop.call_soon_threadsafe(self.spawn, self.followup(body))
        return 200, {"type": 5, "data": {"flags": 64}}

    async def command(self, body):
        state = self.snapshot()
        return "Pop1 Live: private discovery running. " + "; ".join(
            p + (" current" if s["last_success"] and not s["error"] else " waiting for a successful poll")
            for p, s in state["sources"].items())

    async def followup(self, body):
        try:
            try:
                text = await self.command(body)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Discord command: %s", type(exc).__name__)
                text = "Pop1 Live: couldn't finish that request. Please try again."
            await self.discord("POST", f'/webhooks/{self.config["DISCORD_CLIENT_ID"]}/{body["token"]}',
                               dict(message(text), flags=68))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Discord follow-up: %s", type(exc).__name__)

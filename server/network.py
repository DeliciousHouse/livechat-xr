"""Private Pop1 discovery, isolated from hosted chat on the relay's asyncio loop."""
import asyncio
import contextlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

import chat
import backup

log = logging.getLogger("network")
REQUIRED = ("DISCORD_BOT_TOKEN", "DISCORD_PUBLIC_KEY", "POP1_LIVE_APPLICATION_ID", "TWITCH_CLIENT_ID",
            "TWITCH_CLIENT_SECRET", "YOUTUBE_API_KEY", "POP1_LIVE_GUILD_ID", "POP1_LIVE_CHANNEL_ID",
            "POP1_LIVE_FOUNDER_ID")
INTERVALS = {"twitch": 300, "youtube": 900}
GAME = "Population: ONE"
COMMANDS = [
    {"name": "network-status", "description": "Check private Pop1 discovery status", "type": 1},
    {"name": "live", "description": "Small Pop1 streams live now", "type": 1},
    {"name": "optin", "description": "Prove your channel to request a listing", "type": 1, "options": [
        {"name": "platform", "description": "Streaming platform", "type": 3, "required": True,
         "choices": [{"name": n, "value": p} for p, n in (("twitch", "Twitch"), ("youtube", "YouTube"))]},
        {"name": "handle", "description": "Your channel handle", "type": 3, "required": True}]},
    {"name": "profile", "description": "Your listing and free notification preferences", "type": 1, "options": [
        {"name": "ign", "description": "In-game name", "type": 3, "max_length": 64},
        {"name": "tz", "description": "IANA timezone or city", "type": 3, "autocomplete": True},
        {"name": "alerts", "description": "Free similar-size go-live DMs", "type": 5}]},
    {"name": "optout", "description": "Remove your listing immediately", "type": 1},
    {"name": "review", "description": "Founder: review a listing", "type": 1, "options": [
        {"name": "listing", "description": "Listing ID", "type": 4, "required": True},
        {"name": "decision", "description": "Decision", "type": 3, "required": True,
         "choices": [{"name": d, "value": d} for d in ("approve", "reject")]}]},
    {"name": "remove", "description": "Founder: remove a listing", "type": 1, "options": [
        {"name": "listing", "description": "Listing ID", "type": 4, "required": True}]},
]
PACIFIC = ZoneInfo("America/Los_Angeles")
SOURCE_NAMES = {"twitch": "Twitch", "youtube": "YouTube"}
PENDING = "Pop1 Live: got it. A person checks every new listing within 24h — we'll DM you when you're on."
APPROVED = "Pop1 Live: you're on. You'll show up whenever you're live in Population: ONE. /optout anytime."
REJECTED = "Pop1 Live: we can't list this channel right now. Reply here if you think that's a mistake."
REMOVED = "Pop1 Live: removed. You're off the list and pings now; squad offers are cancelled. Notifications people already received can't be pulled back."
BLOCKED_DM = "I couldn't DM you. Server name → Privacy Settings → allow Direct Messages, then run /optin again."
EXPIRED = "Code expired — run /optin again."
FOOTER = "`/live` anytime · `/follow pop1` for pings · `/optin` to get listed\nLive data from Twitch, YouTube · not affiliated"
ZONES = (("Los Angeles", "America/Los_Angeles"), ("Denver", "America/Denver"),
         ("Chicago", "America/Chicago"), ("New York", "America/New_York"),
         ("Toronto", "America/Toronto"), ("São Paulo", "America/Sao_Paulo"),
         ("London", "Europe/London"), ("Paris", "Europe/Paris"), ("Berlin", "Europe/Berlin"),
         ("Tokyo", "Asia/Tokyo"), ("Singapore", "Asia/Singapore"), ("Sydney", "Australia/Sydney"))


def authored(text, *, ephemeral=False):
    """Only authored markdown; external fields must go through message() first."""
    return {"content": text, "allowed_mentions": {"parse": []}, "flags": 68 if ephemeral else 4}


def timezone_choices(query):
    if not isinstance(query, str) or len(query) > 100: return []
    result = [{"name": f"{city} ({zone})", "value": zone} for city, zone in ZONES
              if query.casefold() in (city + " " + zone).casefold()]
    if not result:
        try:
            ZoneInfo(query)
            result.append({"name": query, "value": query})
        except (KeyError, ValueError): pass
    return result[:25]


def similar_size(count, other, *, offline=False):
    if other is None: return False
    if count is None: return offline and 0 <= other <= 4
    def covers(a, b):
        return 0 <= b <= 4 if 0 <= a <= 2 else a / 2 <= b <= a * 2
    return covers(count, other) or covers(other, count)


def safe_destination(url):
    """Strict generated platform URLs only, never a caller-supplied redirect target."""
    if not isinstance(url, str): return None
    if re.fullmatch(r"https://www\.twitch\.tv/[a-z0-9_]{1,25}", url): return url
    if re.fullmatch(r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}", url): return url
    return None


def staged_copy(kind, *, enabled=False, times=(), until=0):
    """Private copy fixtures only: no follow/squad commands, roles or name sharing enabled."""
    if kind == "follow":
        return "Pop1 Live: " + ("Following Pop1 Live — pings at " + ", ".join(f"<t:{int(t)}:t>" for t in times)
            + ". Run /follow pop1 again to stop." if enabled else "Stopped. No more pings.")
    return "Pop1 Live: " + {"squad": f"Looking for squad until <t:{int(until)}:R> · [Stop looking]",
        "already": f"Already looking until <t:{int(until)}:R>",
        "offline": "You need to be live in Pop1 to use /squad.", "expired": "This offer has ended."}[kind]


def staged_ping(rows, list_url, *, viewer_role):
    """DR6A private fixture only. No caller posts it or grants/activates the role."""
    role = message("", viewer_role=viewer_role)
    names = ", ".join(message(name)["content"] + f" ({count})" for name, count in rows)
    payload = authored(f'Pop1 Live: {len(rows)} small streams live — {names}' + role["content"] + "\n" + message(list_url)["content"])
    payload["allowed_mentions"] = role["allowed_mentions"]
    return payload


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
        self.guard = threading.RLock()
        self.peaks = {}

    @contextlib.contextmanager
    def connect(self, *, readonly=False, bounded=False):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro" if readonly else self.path,
                             uri=readonly, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if bounded:
            # Inspection fails unavailable rather than scanning an unbounded cache.
            remaining = 100
            def budget():
                nonlocal remaining
                remaining -= 1
                return remaining <= 0
            db.set_progress_handler(budget, 1000)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.guard, self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2): raise ValueError("unsupported listings schema")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "presence" in tables and (version or "live_streams" in tables):
                raise ValueError("ambiguous presence migration")
            if tables:
                if not version:
                    backup.schema(db, {"listings": backup.DURABLE_V1["listings"] - {"removed_at"},
                                       "channels": backup.EPHEMERAL["channels"] - {"observed_at", "verified"},
                                       "presence": backup.EPHEMERAL["live_streams"]}, version=0)
                else:
                    durable = backup.DURABLE_V1 if version == 1 else backup.DURABLE
                    backup.schema(db, durable if tables == set(durable) else durable | backup.EPHEMERAL, version=version)
            for statement in """
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY, discord_id TEXT NOT NULL DEFAULT '',
                    opted_in INTEGER NOT NULL DEFAULT 0 CHECK(opted_in IN (0,1)),
                    review_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(review_status IN ('pending','reviewed','removed')),
                    dm_closed INTEGER NOT NULL DEFAULT 0 CHECK(dm_closed IN (0,1)), removed_at REAL);
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
                    platform TEXT NOT NULL CHECK(platform IN ('twitch','youtube')),
                    platform_id TEXT NOT NULL, handle TEXT NOT NULL,
                    observed_at REAL NOT NULL DEFAULT 0,
                    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
                    UNIQUE(platform, platform_id));
                CREATE TABLE IF NOT EXISTS supplied_channels (
                    listing_id INTEGER NOT NULL REFERENCES listings(id),
                    platform TEXT NOT NULL CHECK(platform IN ('twitch','youtube')),
                    handle TEXT NOT NULL, UNIQUE(platform, handle))
            """.split(";"):
                if statement.strip(): db.execute(statement)
            if not version:
                if "removed_at" not in {r[1] for r in db.execute("PRAGMA table_info(listings)")}:
                    db.execute("ALTER TABLE listings ADD COLUMN removed_at REAL")
                if "observed_at" not in {r[1] for r in db.execute("PRAGMA table_info(channels)")}:
                    db.execute("ALTER TABLE channels ADD COLUMN observed_at REAL NOT NULL DEFAULT 0")
                if "verified" not in {r[1] for r in db.execute("PRAGMA table_info(channels)")}:
                    db.execute("ALTER TABLE channels ADD COLUMN verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1))")
                if "presence" in tables:
                    db.execute("ALTER TABLE presence RENAME TO live_streams")
                    db.execute("UPDATE channels SET observed_at=COALESCE((SELECT seen_at FROM live_streams WHERE channel_id=channels.id),0)")
            db.execute("""CREATE TABLE IF NOT EXISTS live_streams (
                    channel_id INTEGER PRIMARY KEY REFERENCES channels(id),
                    stream_id TEXT NOT NULL, title TEXT NOT NULL, viewers INTEGER,
                    game TEXT NOT NULL, seen_at REAL NOT NULL, live INTEGER NOT NULL)""")
            if version < 2:
                for column, declaration in (("ign", "TEXT NOT NULL DEFAULT ''"),
                        ("timezone", "TEXT NOT NULL DEFAULT 'America/Los_Angeles'"),
                        ("alerts_enabled", "INTEGER NOT NULL DEFAULT 0 CHECK(alerts_enabled IN (0,1))"),
                        ("would_pay", "INTEGER NOT NULL DEFAULT 0 CHECK(would_pay IN (0,1))")):
                    db.execute(f"ALTER TABLE listings ADD COLUMN {column} {declaration}")
            db.execute("PRAGMA user_version=2")
            self._expire(db, time.time())
            backup.schema(db, backup.DURABLE | backup.EPHEMERAL)
            backup.integrity(db)

    def _delete_channels(self, db, where, args):
        for c in db.execute("SELECT platform,platform_id FROM channels WHERE " + where, args).fetchall():
            self.peaks.pop((c["platform"], c["platform_id"]), None)
        db.execute("DELETE FROM live_streams WHERE channel_id IN (SELECT id FROM channels WHERE " + where + ")", args)
        db.execute("DELETE FROM channels WHERE " + where, args)

    def _expire(self, db, now):
        for r in db.execute("SELECT id FROM listings WHERE removed_at<=? OR (review_status='removed' AND removed_at IS NULL)",
                            (now - 7 * 86400,)).fetchall():
            self._delete_channels(db, "listing_id=?", (r["id"],))
            db.execute("DELETE FROM supplied_channels WHERE listing_id=?", (r["id"],))
            db.execute("DELETE FROM listings WHERE id=?", (r["id"],))
        self._delete_channels(db, "observed_at<=?", (now - 86400,))
        db.execute("DELETE FROM live_streams WHERE seen_at<=? OR live=0", (now - 86400,))
        self.peaks = {k: v for k, v in self.peaks.items() if v[1] > now - 86400}

    def expire(self, now=None):
        with self.guard, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, time.time() if now is None else now)

    def peak(self, platform, platform_id, now=None):
        """None means no record (surfaces use the 0–2 band), not a current count of zero."""
        now = time.time() if now is None else now
        with self.guard:
            peak = self.peaks.get((platform, platform_id))
            if peak and peak[1] > now - 86400: return peak[0]
            self.peaks.pop((platform, platform_id), None)
            return None

    def record_supplied_channel(self, listing_id, platform, handle, *, _db=None):
        """Trusted ownership-verification flow only; never call from a collector."""
        pattern = r"[a-z0-9_]{1,25}" if platform == "twitch" else r"@[\w.-]{3,30}"
        if platform not in INTERVALS or not isinstance(handle, str) or not re.fullmatch(pattern, handle.lower()):
            raise ValueError("invalid supplied handle")
        handle = handle.lower()
        with self.guard, (self.connect() if _db is None else contextlib.nullcontext(_db)) as db:
            if not db.in_transaction: db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM listings WHERE id=?", (listing_id,)).fetchone() is None:
                raise ValueError("unknown listing")
            existing = db.execute("SELECT listing_id FROM supplied_channels WHERE platform=? AND handle=?", (platform, handle)).fetchone()
            if existing and existing[0] != listing_id: raise ValueError("channel already claimed")
            db.execute("INSERT OR IGNORE INTO supplied_channels VALUES (?,?,?)", (listing_id, platform, handle))

    def bind_verified_channel(self, listing_id, platform, handle, platform_id, *, _db=None):
        """After fresh ownership proof, bind a supplied handle to a current collector identity."""
        with self.guard, (self.connect() if _db is None else contextlib.nullcontext(_db)) as db:
            if not db.in_transaction: db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT l.* FROM listings l JOIN supplied_channels s ON s.listing_id=l.id "
                               "WHERE l.id=? AND s.platform=? AND s.handle=?", (listing_id, platform, handle.lower())).fetchone()
            if not owner or owner["removed_at"] is not None or owner["review_status"] == "removed":
                raise ValueError("unverified or removed listing")
            channel = db.execute("SELECT * FROM channels WHERE platform=? AND platform_id=?", (platform, platform_id)).fetchone()
            if not channel: raise ValueError("channel not observed")
            other = db.execute("SELECT id FROM channels WHERE listing_id=? AND platform=? AND verified=1 AND id!=?",
                               (listing_id, platform, channel["id"])).fetchone()
            if other: raise ValueError("listing already has a verified platform binding")
            previous = db.execute("SELECT * FROM listings WHERE id=?", (channel["listing_id"],)).fetchone()
            claimed = db.execute("SELECT 1 FROM supplied_channels WHERE listing_id=?", (channel["listing_id"],)).fetchone()
            if channel["listing_id"] != listing_id and (claimed or previous["opted_in"] or previous["review_status"] != "pending"):
                raise ValueError("channel already bound")
            db.execute("UPDATE channels SET listing_id=?,verified=1 WHERE id=?", (listing_id, channel["id"]))

    def upsert_presence(self, platform, rows, now):
        # A complete successful poll replaces this source's presence, never its consent/review.
        with self.guard, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM live_streams WHERE channel_id IN (SELECT id FROM channels WHERE platform=?)", (platform,))
            for row in rows:
                channel = db.execute("SELECT id FROM channels WHERE platform=? AND platform_id=?",
                                     (platform, row["platform_id"])).fetchone()
                if channel is None:
                    lid = db.execute("INSERT INTO listings DEFAULT VALUES").lastrowid
                    cid = db.execute("INSERT INTO channels(listing_id,platform,platform_id,handle,observed_at) VALUES (?,?,?,?,?)",
                                     (lid, platform, row["platform_id"], row["handle"], now)).lastrowid
                else:
                    cid = channel["id"]
                    db.execute("UPDATE channels SET observed_at=? WHERE id=?", (now, cid))
                    if row["handle"] != row["platform_id"]:
                        db.execute("UPDATE channels SET handle=? WHERE id=?", (row["handle"], cid))
                db.execute("""INSERT INTO live_streams(channel_id,stream_id,title,viewers,game,seen_at,live)
                    VALUES (?,?,?,?,?,?,1)
                    ON CONFLICT(channel_id) DO UPDATE SET stream_id=excluded.stream_id, title=excluded.title,
                    viewers=excluded.viewers, game=excluded.game, seen_at=excluded.seen_at, live=1""",
                    (cid, row["stream_id"], row["title"], row["viewers"], row["game"], now))
                if row["viewers"] is not None:
                    key = (platform, row["platform_id"])
                    old = self.peaks.get(key)
                    if not old or old[1] <= now - 86400 or old[2] != row["stream_id"] or row["viewers"] >= old[0]:
                        self.peaks[key] = (row["viewers"], now, row["stream_id"])

    def set_listing(self, listing_id, *, now=None, **fields):
        if not fields or not fields.keys() <= {"opted_in", "review_status", "discord_id", "dm_closed"}:
            raise ValueError("invalid listing fields")
        now = time.time() if now is None else now
        with self.guard, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            if old is None: raise ValueError("unknown listing")
            if old["removed_at"] is not None and old["removed_at"] <= now - 7 * 86400:
                self._expire(db, now)
                db.commit()
                raise ValueError("expired listing; a new verified opt-in is required")
            if fields.get("review_status") == "removed" or ("opted_in" in fields and not fields["opted_in"]):
                fields.update(opted_in=False, review_status="removed", removed_at=old["removed_at"] if old["removed_at"] is not None else now)
                self._delete_channels(db, "listing_id=?", (listing_id,))
            elif fields.get("opted_in") and old["removed_at"] is not None:
                fields.update(removed_at=None, review_status="pending")
            db.execute("UPDATE listings SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                       (*fields.values(), listing_id))

    def listing(self, listing_id):
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            return dict(row) if row else None

    def channels(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM channels ORDER BY id")]

    def owner_listing(self, discord_id, *, create=False):
        """Caller authenticates the Discord ID. Lock serializes find/create across HTTP/loop."""
        if not isinstance(discord_id, str) or not re.fullmatch(r"\d{1,22}", discord_id):
            raise ValueError("invalid owner")
        with self.guard, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, time.time())
            rows = db.execute("SELECT * FROM listings WHERE discord_id=?", (discord_id,)).fetchall()
            if len(rows) > 1: raise ValueError("ambiguous owner; founder reconciliation required")
            if rows: return dict(rows[0])
            if create:
                lid = db.execute("INSERT INTO listings(discord_id,dm_closed) VALUES (?,1)", (discord_id,)).lastrowid
                return dict(db.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone())
            return None

    def profile(self, listing_id):
        """Detached private owner data. Authorization belongs at each command/HTTP boundary."""
        return self.listing(listing_id)

    def update_profile(self, listing_id, *, ign=None, timezone=None, alerts_enabled=None, would_pay=None):
        fields = {k: v for k, v in locals().items() if k in ("ign", "timezone", "alerts_enabled", "would_pay") and v is not None}
        if ign is not None and (not isinstance(ign, str) or not 1 <= len(ign) <= 64
                or not ign.isprintable() or "@" in ign or "<" in ign or ">" in ign):
            raise ValueError("IGN must be 1–64 printable characters without mentions")
        if timezone is not None:
            try: ZoneInfo(timezone)
            except (ValueError, TypeError, KeyError): raise ValueError("Use a valid IANA timezone") from None
        if any(type(v) is not bool for v in (alerts_enabled, would_pay) if v is not None):
            raise ValueError("preferences must be boolean")
        with self.guard, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM listings WHERE id=?", (listing_id,)).fetchone():
                raise ValueError("unknown listing")
            if fields:
                db.execute("UPDATE listings SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), listing_id))

    def demand(self):
        with self.connect() as db:
            return {"total": db.execute("SELECT COUNT(*) FROM listings WHERE would_pay=1 AND discord_id!=''").fetchone()[0],
                    "outside_cohort": None}

    def matching_count(self, listing_id, now=None):
        now = time.time() if now is None else now
        live = [r for r in self.live_channels(now) if r["listing_id"] == listing_id]
        if live: return live[0]["viewers"], False
        with self.guard:
            channels = sorted((c for c in self.channels() if c["listing_id"] == listing_id and c["verified"]),
                              key=lambda c: c["platform"])
            if not channels: return None, True
            primary = channels[0]
            return self.peak(primary["platform"], primary["platform_id"], now), True

    def inspection_rows(self, now=None):
        """Public presentation only; no initialization, expiry writes or provider calls."""
        now = time.time() if now is None else now
        if not self.path.is_file(): raise ValueError("cache unavailable")
        with self.guard:
            with self.connect(readonly=True, bounded=True) as db:
                missing = db.execute("""SELECT 1 FROM live_streams p JOIN channels c ON c.id=p.channel_id
                    JOIN listings l ON l.id=c.listing_id
                    WHERE l.opted_in=1 AND l.review_status='reviewed' AND c.verified=1 AND p.live=1
                    AND (p.seen_at IS NULL OR p.seen_at<=0 OR p.seen_at>? OR
                         c.observed_at IS NULL OR c.observed_at<=0 OR c.observed_at>?) LIMIT 1""", (now, now)).fetchone()
                if missing: raise ValueError("observation unavailable")
            channels = self.live_channels(now, limit=201)
            if len(channels) > 200: raise ValueError("inspection capacity exceeded")
            primary = self.discovery(now, limit=201)
            rows = []
            for r in primary:
                profile = self.profile(r["listing_id"])
                if not profile or not profile["opted_in"] or profile["review_status"] != "reviewed": continue
                public = []
                for c in channels:
                    if c["listing_id"] != r["listing_id"] or c["game"] != GAME: continue
                    if any(not isinstance(c[k], (int, float)) or not math.isfinite(c[k])
                           or not 0 < c[k] <= now for k in ("seen_at", "observed_at")):
                        raise ValueError("observation unavailable")
                    if now - c["observed_at"] >= 86400: raise ValueError("expired channel observation")
                    destination = safe_destination("https://www.twitch.tv/" + c["handle"]
                        if c["platform"] == "twitch" else "https://www.youtube.com/watch?v=" + c["stream_id"])
                    if not destination: raise ValueError("unsafe source destination")
                    public.append({"platform": c["platform"], "viewers": c["viewers"],
                        "seen_at": c["seen_at"], "observed_at": c["observed_at"], "destination": destination})
                if public: rows.append({"name": profile["ign"] or r["handle"], "channels": public})
            first = {}
            for c in channels: first.setdefault(c["listing_id"], c)
            return {"rows": rows, "unknown_primary_count": sum(c["viewers"] is None for c in first.values())}

    def live_channels(self, now=None, *, limit=None):
        """Verified, consenting live channels with raw per-platform counts (including None)."""
        now = time.time() if now is None else now
        with self.connect(readonly=True, bounded=limit is not None) as db:
            return [dict(r) for r in db.execute("""SELECT c.*, p.title,p.viewers,p.stream_id,p.seen_at,p.game
                FROM channels c JOIN listings l ON l.id=c.listing_id JOIN live_streams p ON p.channel_id=c.id
                WHERE l.opted_in=1 AND l.review_status='reviewed' AND c.verified=1 AND p.live=1
                AND p.seen_at>? ORDER BY c.listing_id,c.platform,c.id LIMIT ?""", (now - 1800, -1 if limit is None else limit))]

    def discovery(self, now=None, *, limit=None):
        # Twitch sorts before YouTube. Choose live primary BEFORE examining its count.
        primary = {}
        for row in self.live_channels(now, limit=limit): primary.setdefault(row["listing_id"], row)
        return sorted((r for r in primary.values() if r["game"] == GAME and r["viewers"] is not None
                       and 0 <= r["viewers"] <= 10), key=lambda r: (r["viewers"], r["id"]))

    def dm_closed_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM listings WHERE dm_closed=1").fetchone()[0]


DISCORD_OPERATIONS = (
    ("PATCH", r"/webhooks/[^/]+/[^/]+/messages/@original", "edit_original"),
    ("PUT", r"/applications/[^/]+/guilds/[^/]+/commands", "register_commands"),
    ("POST", r"/users/@me/channels", "open_dm"),
    ("POST", r"/channels/[^/]+/messages", "create_message"),
    ("PATCH", r"/channels/[^/]+/messages/[^/]+", "edit_message"),
    ("GET", r"/channels/[^/]+/messages\?.*", "list_messages"),
)


class HTTPFailure(Exception):
    def __init__(self, status, retry_after=0, quota=False, *, operation=None, discord_code=None):
        self.status, self.retry_after, self.quota = status, retry_after, quota
        self.discord_code = discord_code if type(discord_code) is int and 0 <= discord_code <= 2147483647 else None
        self.operation = operation if operation in {op for _, _, op in DISCORD_OPERATIONS} else "other"
        if operation is not None:
            self.status = status if type(status) is int and 100 <= status <= 599 else None
            super().__init__(f"Discord operation={self.operation} status={self.status} code={self.discord_code}")
        else:
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
        self.started_at = time.time()
        self.sources = {p: {"last_success": 0, "error": "", "success_streak": 0,
                            "delayed": False, "incident_since": None} for p in INTERVALS}
        self.seen_interactions = {}
        self.challenges = {}
        self.buttons = {}
        self.go = {}
        self.live_message = None
        self.list_checked = False
        self.list_lookup_retries = 0
        self.naming_messages = {}
        self.reconcile = {}
        self.last_post = 0
        self.alert_edges = {}
        self.surface_lock = asyncio.Lock()

    def snapshot(self):
        with self.guard:
            return {"enabled": self.enabled, "missing": list(self.missing), "error": self.error,
                    "sources": {p: dict(s) for p, s in self.sources.items()}}

    def record_poll(self, platform, *, error=None, now=None):
        now = time.time() if now is None else now
        with self.guard:
            state = self.sources[platform]
            if error is None:
                state.update(last_success=now, error="", success_streak=state["success_streak"] + 1)
            else:
                reason = str(error) if isinstance(error, HTTPFailure) else type(error).__name__
                state.update(error=reason, success_streak=0)

    async def notify_founder(self, text):
        channel = await self.discord("POST", "/users/@me/channels", {"recipient_id": self.config["POP1_LIVE_FOUNDER_ID"]})
        if channel.get("dm_closed"):
            log.warning("Founder incident DM: DMs closed")
            return
        result = await self.discord("POST", f'/channels/{channel["id"]}/messages', message("Pop1 Live: " + text))
        if result.get("dm_closed"): log.warning("Founder incident DM: DMs closed")

    async def check_incidents(self, now=None):
        now = time.time() if now is None else now
        notices = []
        with self.guard:
            if not self.enabled: return
            in_window = 18 <= datetime.fromtimestamp(now, PACIFIC).hour < 23
            for platform, state in self.sources.items():
                since = state["last_success"] or self.started_at
                if state["delayed"] and state["success_streak"] >= 2 and now - since < 900:
                    state.update(delayed=False, incident_since=None)
                    notices.append(SOURCE_NAMES[platform] + " collector recovered after two successful polls. /admin")
                elif not state["delayed"] and in_window and now - since >= 900:
                    state.update(delayed=True, incident_since=since, success_streak=0)
                    stamp = datetime.fromtimestamp(since, PACIFIC).strftime("%H:%M %Z")
                    notices.append(f'{SOURCE_NAMES[platform]} collector stale since {stamp} ({state["error"] or "no successful poll"}). /admin')
        for text in notices:
            try:
                await self.notify_founder(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Founder incident DM: %s", type(exc).__name__)

    async def maintain(self):
        while not self.stopping.is_set():
            try:
                await self.offload(self.store.expire)
                await self.check_incidents()
                await self.surface_tick()
                await self.check_alerts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Discovery maintenance: %s", type(exc).__name__)
            await asyncio.sleep(1)

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
        self.started_at = time.time()
        self.enabled = True
        for platform in INTERVALS:
            self.spawn(self.poll(platform))
        self.spawn(self.register_commands())
        self.spawn(self.maintain())

    async def stop(self):
        self.enabled = False
        self.stopping.set()
        for challenge in self.challenges.values(): challenge["active"] = False
        self.challenges.clear()
        self.buttons.clear()
        with self.guard: self.go.clear()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*list(self.io_tasks), return_exceptions=True)
        self.tasks.clear()
        if self.live_message:
            try:
                await asyncio.wait_for(self.discord("PATCH", f'/channels/{self.live_message[0]}/messages/{self.live_message[1]}',
                                                   authored("Pop1 Live is paused — back soon.")), timeout=12)
            except Exception:
                log.warning("Discovery stop: final paused edit failed")
            await asyncio.gather(*list(self.io_tasks), return_exceptions=True)

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
                self.record_poll(platform, now=now)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                reason = str(exc) if isinstance(exc, HTTPFailure) else type(exc).__name__
                self.record_poll(platform, error=exc)
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
            body = body if isinstance(body, dict) else {}
            error = body.get("error")
            errors = error.get("errors") if isinstance(error, dict) else None
            quota = isinstance(errors, list) and any(
                isinstance(e, dict) and isinstance(e.get("reason"), str)
                and e["reason"] in ("quotaExceeded", "dailyLimitExceeded") for e in errors)
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
        headers = {"Content-Type": "application/json",
                   "User-Agent": "DiscordBot (https://livechat.deliciouswines.org, 1.0)"}
        if not path.startswith("/webhooks/"):
            headers["Authorization"] = "Bot " + self.config["DISCORD_BOT_TOKEN"]
        return request_json("https://discord.com/api/v10" + path, headers=headers, data=payload, method=method)

    async def discord(self, method, path, payload):
        for attempt in range(4):
            code, body = await self.offload(self._discord, method, path, payload)
            error = body if isinstance(body, dict) else {}
            if code == 429 and attempt < 3:
                await wait_retry(retry_delay(error.get("retry_after", 60)))
                continue
            if error.get("code") == 50007:
                return {"dm_closed": True}
            if not 200 <= code < 300:
                operation = next((op for verb, pattern, op in DISCORD_OPERATIONS
                                  if method == verb and re.fullmatch(pattern, path)), "other")
                failure = HTTPFailure(code, operation=operation, discord_code=error.get("code"))
                log.warning("%s", failure)
                raise failure
            return body

    async def register_commands(self):
        while not self.stopping.is_set():
            try:
                await self.discord("PUT", f'/applications/{self.config["POP1_LIVE_APPLICATION_ID"]}/guilds/{self.config["POP1_LIVE_GUILD_ID"]}/commands', COMMANDS)
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
            channel = await self.discord("POST", f'/channels/{channel["id"]}/messages', message(text if text.startswith("Pop1 Live:") else "Pop1 Live: " + text))
        if channel.get("dm_closed"):
            await self.offload(self.store.set_listing, listing_id, dm_closed=True)
            return False
        return True

    def go_link(self, channel):
        destination = ("https://www.twitch.tv/" + channel["handle"] if channel["platform"] == "twitch"
                       else "https://www.youtube.com/watch?v=" + channel["stream_id"])
        if not safe_destination(destination): return None
        key = (channel["listing_id"], channel["platform"], channel["stream_id"], destination)
        with self.guard:
            now = time.time()
            self.go = {k: v for k, v in self.go.items() if v["expires"] > now}
            opaque = next((k for k, v in self.go.items() if v["key"] == key), None)
            if opaque is None:
                if len(self.go) >= 10000: return None
                opaque = secrets.token_urlsafe(16)
                self.go[opaque] = {"key": key, "expires": min(now + 86400, channel["seen_at"] + 1800), "posted": False}
            else:
                self.go[opaque]["expires"] = min(now + 86400, channel["seen_at"] + 1800)
        return self.config.get("PUBLIC_URL", "https://livechat.deliciouswines.org").rstrip("/") + "/go/" + opaque

    def resolve_go(self, opaque_id, *, limit=None):
        if not self.enabled or not isinstance(opaque_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", opaque_id): return None
        with self.guard:
            entry = self.go.get(opaque_id)
            if not entry or entry["expires"] <= time.time(): return None
            lid, platform, stream, destination = entry["key"]
        # Recheck durable consent and current live state even if removal came through a future HTTP caller.
        for c in self.store.live_channels(limit=limit):
            if (c["listing_id"], c["platform"], c["stream_id"]) == (lid, platform, stream):
                expected = ("https://www.twitch.tv/" + c["handle"] if platform == "twitch"
                            else "https://www.youtube.com/watch?v=" + c["stream_id"])
                if expected == destination: return safe_destination(destination)
        return None

    async def render_live(self, now=None, *, scheduled=False, inspection=False):
        now = time.time() if now is None else now
        state = self.snapshot()
        if not state["enabled"]: return authored("Pop1 Live is paused — back soon.")
        local = datetime.fromtimestamp(now, PACIFIC)
        if scheduled and not 18 <= local.hour < 23:
            start = int(local.replace(hour=18, minute=0, second=0, microsecond=0).timestamp())
            end = int(local.replace(hour=23, minute=0, second=0, microsecond=0).timestamp())
            return authored(f"The list updates <t:{start}:t>–<t:{end}:t> each night (your time). /live works anytime.")
        primary = await self.offload(self.store.discovery, now, **({"limit": 201} if inspection else {}))
        channels = await self.offload(self.store.live_channels, now, **({"limit": 201} if inspection else {}))
        stamps = [int(s["last_success"]) for s in state["sources"].values() if s["last_success"]]
        stamp = min(stamps) if stamps else int(self.started_at)
        warnings = [f'⚠ {SOURCE_NAMES[p]} streams delayed since <t:{int(s["incident_since"] or self.started_at)}:R> · others current'
                    for p, s in state["sources"].items() if s["delayed"]]
        if len(warnings) == len(INTERVALS):
            warnings = [f'⚠ Live data delayed since <t:{stamp}:R> — /live may be out of date too']
        if inspection:
            warnings = []  # The inspector carries exact poll/observation provenance separately.
        if not primary:
            if inspection: return authored("No eligible small streams in the observed cache.")
            return authored("\n".join(warnings + [f"No small Pop1 streams live right now · updated <t:{stamp}:R>. Going live? /optin to be listed."]))
        header = (f"**Pop1 Live** · {len(primary)} cached small streams" if inspection else
                  f"**Pop1 Live** · {len(primary)} small streams live · updated <t:{stamp}:R>")
        freshness = " · ".join(f'{SOURCE_NAMES[p]} updated <t:{int(s["last_success"])}:R>' if s["last_success"]
                               else f'{SOURCE_NAMES[p]} waiting for a successful poll' for p, s in state["sources"].items())
        rows = []
        for r in primary:
            profile = await self.offload(self.store.profile, r["listing_id"])
            if not profile or not profile["opted_in"] or profile["review_status"] != "reviewed": continue
            name = message(profile["ign"] or r["handle"])["content"]
            live = [c for c in channels if c["listing_id"] == r["listing_id"] and c["game"] == GAME]
            counts, links = [], []
            for c in live:
                label = SOURCE_NAMES[c["platform"]]
                counts.append(label + (" viewers: " if inspection else " ") + (str(c["viewers"]) if c["viewers"] is not None else "unknown"))
                url = self.go_link(c)
                if url: links.append(f"[Watch on {label}]({url})")
            age = max(0, int((now - r["seen_at"]) // 60))
            rows.append(" · ".join([f"**{name}**"] + counts + links) + (f" · last seen {age} min ago" if age >= 15 else ""))
        prefix = header if inspection else "\n".join(warnings + [header, freshness])
        footer = "Cached source observations · not affiliated" if inspection else FOOTER
        visible = []
        for index, row_text in enumerate(rows):
            overflow = f"+{len(rows) - index} more cached streams" if inspection else f"+{len(rows) - index} more — /live for all"
            if len("\n".join([prefix] + visible + [row_text, overflow, footer]).encode("utf-16-le")) // 2 > 2000:
                visible.append(overflow)
                break
            visible.append(row_text)
        return authored("\n".join([prefix] + visible + [footer]))

    async def locate_list(self):
        if self.list_checked: return
        channel = self.config["POP1_LIVE_CHANNEL_ID"]
        cursor = ""
        for _ in range(10):
            previous = await self.discord("GET", f"/channels/{channel}/messages?limit=100" + cursor, None)
            if not isinstance(previous, list): raise ValueError("invalid message history")
            for item in previous:
                text = item.get("content", "")
                if item.get("author", {}).get("id") == self.config["POP1_LIVE_APPLICATION_ID"] and (
                        text.startswith("**Pop1 Live**") or text.startswith("No small Pop1 streams")
                        or text.startswith("The list updates") or text.startswith("Pop1 Live is paused")
                        or text.startswith("⚠ ")):
                    self.live_message = (channel, item["id"])
                    self.list_checked = True
                    return
            if len(previous) < 100:
                self.list_checked = True
                return
            cursor = "&before=" + previous[-1]["id"]
        raise ValueError("list history exceeds bounded lookup; founder reconciliation required")

    async def surface_tick(self, now=None):
        now = time.time() if now is None else now
        async with self.surface_lock:
            if self.list_lookup_retries:
                self.list_lookup_retries -= 1
                try:
                    await self.locate_list()
                    if self.live_message: self.reconcile[self.live_message] = 0
                    self.list_lookup_retries = 0
                except Exception:
                    log.warning("Discovery list lookup reconciliation failed")
            for target, attempts in list(self.reconcile.items()):
                try:
                    payload = await self.render_live(now) if target == self.live_message else authored("Pop1 Live: this ping is no longer current. /live for the list.")
                    await self.discord("PATCH", f"/channels/{target[0]}/messages/{target[1]}", payload)
                    self.reconcile.pop(target, None)
                except Exception:
                    if attempts >= 3: self.reconcile.pop(target, None)
                    else: self.reconcile[target] = attempts + 1
            in_window = 18 <= datetime.fromtimestamp(now, PACIFIC).hour < 23
            if not self.enabled or now - self.last_post < 300: return
            if not in_window and not self.live_message: return
            payload = await self.render_live(now, scheduled=True)
            channel = self.config["POP1_LIVE_CHANNEL_ID"]
            await self.locate_list()
            if self.live_message:
                await self.discord("PATCH", f"/channels/{channel}/messages/{self.live_message[1]}", payload)
            else:
                result = await self.discord("POST", f"/channels/{channel}/messages", payload)
                self.live_message = (channel, result["id"])
            with self.guard:
                for opaque, entry in self.go.items():
                    if "/go/" + opaque in payload["content"]: entry["posted"] = True
            self.last_post = now

    async def remove_listing(self, listing_id, *, reject=False):
        listing = await self.offload(self.store.listing, listing_id)
        if not listing: raise ValueError("unknown listing")
        challenge = self.challenges.pop(listing["discord_id"], None)
        if challenge:
            challenge["active"] = False
            if challenge.get("task"): challenge["task"].cancel()
        await self.offload(self.store.set_listing, listing_id, opted_in=False)
        with self.guard: self.go = {k: v for k, v in self.go.items() if v["key"][0] != listing_id}
        self.buttons = {k: v for k, v in self.buttons.items() if v[0] != listing_id}
        self.alert_edges = {k: v for k, v in self.alert_edges.items() if listing_id not in k[:2]}
        okay = True
        async with self.surface_lock:
            try:
                await self.locate_list()
            except Exception:
                okay = False
                self.list_lookup_retries = 3
            targets = {k for k, lids in self.naming_messages.items() if listing_id in lids}
            if self.live_message: targets.add(self.live_message)
            for target in targets:
                payload = await self.render_live() if target == self.live_message else authored("Pop1 Live: this ping is no longer current. /live for the list.")
                try:
                    await self.discord("PATCH", f"/channels/{target[0]}/messages/{target[1]}", payload)
                    self.reconcile.pop(target, None)
                    self.naming_messages.pop(target, None)
                except Exception:
                    okay = False
                    self.reconcile[target] = 0
        try:
            if reject: await self.send_dm(listing_id, REJECTED)
            await self.send_dm(listing_id, REMOVED if okay else "Pop1 Live: locally removed. Some remote edits failed; bounded reconciliation queued.")
        except Exception:
            okay = False
        return okay

    async def check_alerts(self):
        if not self.enabled: return
        now = time.time()
        self.alert_edges = {k: v for k, v in self.alert_edges.items() if v > now}
        live = await self.offload(self.store.live_channels, now)
        primary = {}
        for stream in live: primary.setdefault(stream["listing_id"], stream)
        streams = [s for s in primary.values() if s["game"] == GAME and s["viewers"] is not None and s["viewers"] >= 0]
        channels = await self.offload(self.store.channels)
        recipients = {c["listing_id"] for c in channels if c["verified"]}
        for stream in streams:
            sender = await self.offload(self.store.profile, stream["listing_id"])
            if not sender or not sender["ign"]: continue
            for lid in recipients - {stream["listing_id"]}:
                edge = (lid, stream["listing_id"], stream["platform"], stream["stream_id"])
                if edge in self.alert_edges: continue
                recipient = await self.offload(self.store.profile, lid)
                if not recipient or not (recipient["opted_in"] and recipient["review_status"] == "reviewed"
                        and recipient["alerts_enabled"] and recipient["ign"] and not recipient["dm_closed"]): continue
                count, offline = await self.offload(self.store.matching_count, lid, now)
                if not similar_size(count, stream["viewers"], offline=offline): continue
                # Consume before transport: a failed/blocked DM must not flood every maintenance tick.
                self.alert_edges[edge] = now + 86400
                async with self.surface_lock:
                    active = await self.offload(self.store.live_channels, now)
                    recipient = await self.offload(self.store.profile, lid)
                    if not recipient or not (recipient["opted_in"] and recipient["review_status"] == "reviewed"
                            and recipient["alerts_enabled"] and recipient["ign"] and not recipient["dm_closed"]) or stream not in active: continue
                    await self.send_dm(lid, f'{sender["ign"]} just went live on {SOURCE_NAMES[stream["platform"]]} with {stream["viewers"]} viewers — about your size.')


    def demand_button(self, listing_id, owner):
        now = time.time()
        self.buttons = {k: v for k, v in self.buttons.items() if v[2] > now}
        key = next((k for k, v in self.buttons.items() if v[:2] == (listing_id, owner)), None)
        if key is None:
            if len(self.buttons) >= 10000: raise ValueError("too many active buttons")
            key = secrets.token_urlsafe(16)
        self.buttons[key] = (listing_id, owner, now + 86400)
        return {"type": 1, "components": [{"type": 2, "style": 2, "label": "Yes, I'd pay", "custom_id": key}]}

    async def welcome(self, listing_id):
        listing = await self.offload(self.store.profile, listing_id)
        if not listing or not listing["opted_in"]: return False
        # Retry intentionally ignores the closed flag, but only successful delivery clears it.
        await self.offload(self.store.set_listing, listing_id, dm_closed=True)
        channel = await self.discord("POST", "/users/@me/channels", {"recipient_id": listing["discord_id"]})
        if channel.get("dm_closed"): return False
        status = APPROVED if listing["review_status"] == "reviewed" else PENDING
        payload = authored(status + "\nSet your in-game name and timezone with /profile. Go-live DMs are free and off until you opt in.\nWould you pay $3/mo to be pinned at the top?")
        payload["components"] = [self.demand_button(listing_id, listing["discord_id"])]
        result = await self.discord("POST", f'/channels/{channel["id"]}/messages', payload)
        if result.get("dm_closed"): return False
        await self.offload(self.store.set_listing, listing_id, dm_closed=False)
        return True

    async def edit_original(self, body, payload):
        await self.discord("PATCH", f'/webhooks/{self.config["POP1_LIVE_APPLICATION_ID"]}/{body["token"]}/messages/@original', payload)

    async def begin_challenge(self, body, owner, fields, listing):
        platform, supplied = fields.get("platform"), fields.get("handle")
        if platform not in INTERVALS or not isinstance(supplied, str): raise ValueError("choose Twitch or YouTube")
        handle = self.normalise_channel(platform, supplied).lower()
        pattern = r"[a-z0-9_]{1,25}" if platform == "twitch" else r"@[\w.-]{3,30}"
        if not re.fullmatch(pattern, handle): raise ValueError("supply a channel handle")
        identity = await self.resolve_identity(platform, handle)
        if not identity or not identity.get("id"): raise ValueError("channel not found")
        if listing and listing["opted_in"] and any(c["listing_id"] == listing["id"] and c["platform"] == platform
                and c["platform_id"] == identity["id"] and c["verified"] for c in await self.offload(self.store.channels)):
            delivered = await self.welcome(listing["id"])
            return authored("Verified ✓ You can remove the code now." + ("" if delivered else "\n" + BLOCKED_DM))
        previous = self.challenges.pop(owner, None)
        if previous:
            previous["active"] = False
            if previous.get("task"): previous["task"].cancel()
        if len(self.challenges) >= 1000: raise ValueError("too many active challenges")
        challenge = dict(owner=owner, platform=platform, handle=handle, platform_id=identity["id"],
            code=secrets.token_hex(16), deadline=body.get("_received_at", time.monotonic()) + 900, interaction=body, active=True)
        self.challenges[owner] = challenge
        where = "Twitch title or bio" if platform == "twitch" else "YouTube channel description or live title"
        return authored(f'```\n{challenge["code"]}\n```\nPut this in your {where} — I\'ll check every minute.\nValid for 15 minutes.')

    async def proof_visible(self, platform, handle, platform_id, code, video_ids=None):
        if platform == "twitch":
            token = await self.offload(chat._twitch_access)
            headers = {"Client-ID": self.config["TWITCH_CLIENT_ID"], "Authorization": "Bearer " + token}
            users = await self.offload(self.api, "https://api.twitch.tv/helix/users", {"login": handle}, headers)
            user = next((u for u in users["data"] if u["id"] == platform_id), None)
            if not user: return False
            channels = await self.offload(self.api, "https://api.twitch.tv/helix/channels", {"broadcaster_id": platform_id}, headers)
            return code in user.get("description", "") or any(code in c.get("title", "") for c in channels["data"])
        params = dict(part="snippet", id=platform_id, key=self.config["YOUTUBE_API_KEY"])
        body = await self.offload(self.api, "https://www.googleapis.com/youtube/v3/channels", params)
        if any(c["id"] == platform_id and code in c.get("snippet", {}).get("description", "") for c in body["items"]): return True
        if video_ids is None:
            def observed_videos():
                with self.store.connect() as db:
                    return [r[0] for r in db.execute("SELECT p.stream_id FROM live_streams p JOIN channels c ON c.id=p.channel_id "
                        "WHERE c.platform='youtube' AND c.platform_id=? AND p.seen_at>?", (platform_id, time.time() - 1800))]
            video_ids = await self.offload(observed_videos)
        if not video_ids: return False
        body = await self.offload(self.api, "https://www.googleapis.com/youtube/v3/videos", dict(
            part="snippet,liveStreamingDetails", id=",".join(video_ids), key=self.config["YOUTUBE_API_KEY"]))
        return any(v.get("snippet", {}).get("channelId") == platform_id and code in v["snippet"].get("title", "")
            and v.get("liveStreamingDetails", {}).get("actualStartTime") and not v["liveStreamingDetails"].get("actualEndTime")
            for v in body["items"])

    def finish_proof(self, challenge):
        # Same lock as collector/removal; a cancelled worker thread cannot revive consent.
        s = self.store
        with s.guard, s.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.challenge_active(challenge): raise ValueError("cancelled proof")
            owner, platform, handle, pid = (challenge[k] for k in ("owner", "platform", "handle", "platform_id"))
            s._expire(db, time.time())
            listings = db.execute("SELECT * FROM listings WHERE discord_id=?", (owner,)).fetchall()
            if len(listings) > 1: raise ValueError("ambiguous owner")
            if listings:
                lid = listings[0]["id"]
                db.execute("UPDATE listings SET opted_in=1,dm_closed=1,removed_at=NULL,review_status=? WHERE id=?",
                    ("pending" if listings[0]["review_status"] == "removed" else listings[0]["review_status"], lid))
            else:
                lid = db.execute("INSERT INTO listings(discord_id,opted_in,dm_closed) VALUES (?,1,1)", (owner,)).lastrowid
            # Official proof may be offline: observe identity only, never invent a live row/count.
            channel = db.execute("SELECT * FROM channels WHERE platform=? AND platform_id=?", (platform, pid)).fetchone()
            if channel is None:
                shell = db.execute("INSERT INTO listings DEFAULT VALUES").lastrowid
                db.execute("INSERT INTO channels(listing_id,platform,platform_id,handle,observed_at) VALUES (?,?,?,?,?)",
                           (shell, platform, pid, handle, time.time()))
            s.record_supplied_channel(lid, platform, handle, _db=db)
            s.bind_verified_channel(lid, platform, handle, pid, _db=db)
            if not self.challenge_active(challenge): raise ValueError("cancelled proof")
            return lid

    def challenge_active(self, challenge):
        return (challenge["active"] and not self.stopping.is_set() and self.challenges.get(challenge["owner"]) is challenge
                and time.monotonic() < challenge["deadline"])

    async def check_challenge(self, challenge):
        body = challenge["interaction"]
        try:
            while self.challenge_active(challenge):
                await wait_retry(min(60, max(0, challenge["deadline"] - time.monotonic())))
                if not self.challenge_active(challenge): break
                text = "Checking… not found yet (titles can take a minute)"
                try:
                    identity = await self.resolve_identity(challenge["platform"], challenge["handle"])
                    if not identity or identity.get("id") != challenge["platform_id"]:
                        text = "That code is on a different channel than @" + challenge["handle"].lstrip("@")
                    elif await self.proof_visible(challenge["platform"], challenge["handle"], challenge["platform_id"], challenge["code"]):
                        # Re-resolve after reading proof to catch reassigned handles during an API round trip.
                        current = await self.resolve_identity(challenge["platform"], challenge["handle"])
                        if not current or current.get("id") != challenge["platform_id"]:
                            text = "That code is on a different channel than @" + challenge["handle"].lstrip("@")
                        elif self.challenge_active(challenge):
                            lid = await self.offload(self.finish_proof, challenge)
                            try:
                                delivered = await self.welcome(lid)
                            except Exception:
                                delivered = False
                            await self.edit_original(body, authored("Verified ✓ You can remove the code now." + ("" if delivered else "\n" + BLOCKED_DM), ephemeral=True))
                            return
                except asyncio.CancelledError: raise
                except Exception:
                    text = "Checking… couldn't reach the platform. I'll try again next minute."
                if self.challenge_active(challenge): await self.edit_original(body, authored(text, ephemeral=True))
            if challenge["active"] and self.challenges.get(challenge["owner"]) is challenge:
                await self.edit_original(body, authored(EXPIRED, ephemeral=True))
        finally:
            challenge["active"] = False
            if self.challenges.get(challenge["owner"]) is challenge: self.challenges.pop(challenge["owner"], None)

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
            elif user.get("id") != self.config["POP1_LIVE_FOUNDER_ID"] and body.get("type") != 3:
                return 403, {}
            if body.get("application_id") != self.config["POP1_LIVE_APPLICATION_ID"]: return 403, {}
            if not isinstance(user.get("id"), str) or not re.fullmatch(r"\d{1,22}", user["id"]): return 403, {}
            kind = body.get("type")
            if kind not in (2, 3, 4) or (kind != 3 and body.get("data", {}).get("name") not in {c["name"] for c in COMMANDS}):
                return 200, {"type": 4, "data": dict(message("This command is not available in the private test."), flags=64)}
            if not all(isinstance(body.get(k), str) and re.fullmatch(r"[\w.-]{1,256}", body[k]) for k in ("id", "token")):
                return 400, {}
            if kind == 3:
                button = self.buttons.get(body.get("data", {}).get("custom_id"))
                if button and button[1] != user["id"]: return 403, {}
                if not button or button[2] <= time.time():
                    return 200, {"type": 4, "data": authored("Pop1 Live: This offer has ended.", ephemeral=True)}
            if kind == 4:
                options = body.get("data", {}).get("options", [])
                if not isinstance(options, list) or any(not isinstance(o, dict) for o in options): return 400, {}
        except (AttributeError, TypeError):
            return 400, {}
        with self.guard:
            now = time.time()
            self.seen_interactions = {k: t for k, t in self.seen_interactions.items() if t > now - 300}
            if body["id"] in self.seen_interactions: return 409, {}
            if len(self.seen_interactions) >= 1000: return 429, {}
            self.seen_interactions[body["id"]] = now
            if kind == 4:
                options = body.get("data", {}).get("options", [])
                query = next((o.get("value", "") for o in options if o.get("name") == "tz" and o.get("focused")), "")
                choices = timezone_choices(query) if body["data"]["name"] == "profile" else []
                return 200, {"type": 8, "data": {"choices": choices}}
            body["_received_at"] = time.monotonic()
            self.loop.call_soon_threadsafe(self.spawn, self.followup(body))
        return 200, {"type": 5, "data": {"flags": 64}}

    async def command(self, body):
        user = (body.get("member", {}).get("user", {}) if body.get("guild_id") else body.get("user", {}))["id"]
        if body.get("type") == 3:
            entry = self.buttons.get(body.get("data", {}).get("custom_id"))
            if not entry or entry[2] <= time.time(): return authored("Pop1 Live: This offer has ended.")
            lid, owner, _ = entry
            if owner != user: raise PermissionError("button owner mismatch")
            listing = await self.offload(self.store.owner_listing, user)
            if not listing or listing["id"] != lid: raise PermissionError("listing owner mismatch")
            await self.offload(self.store.update_profile, lid, would_pay=True)
            return authored("Pop1 Live: thanks — counted once. Nothing is charged.")
        name = body["data"]["name"]
        options = body["data"].get("options", [])
        if not isinstance(options, list) or any(not isinstance(o, dict) for o in options): raise ValueError("invalid options")
        fields = {o["name"]: o.get("value") for o in options}
        allowed = {"optin": {"platform", "handle"}, "profile": {"ign", "tz", "alerts"},
                   "review": {"listing", "decision"}, "remove": {"listing"}}
        if not fields.keys() <= allowed.get(name, set()): raise ValueError("invalid command fields")
        if name == "live": return await self.render_live()
        if name in ("review", "remove"):
            if user != self.config["POP1_LIVE_FOUNDER_ID"]: raise PermissionError("founder only")
            lid = fields.get("listing")
            if type(lid) is not int or not await self.offload(self.store.listing, lid): raise ValueError("unknown listing")
            if name == "remove" or fields.get("decision") == "reject":
                okay = await self.remove_listing(lid, reject=name == "review")
                return authored(REMOVED if okay else "Pop1 Live: locally removed. Some remote edits failed; bounded reconciliation queued.")
            if fields.get("decision") != "approve": raise ValueError("invalid decision")
            await self.offload(self.store.set_listing, lid, review_status="reviewed")
            await self.send_dm(lid, APPROVED)
            return authored(APPROVED)
        if name == "optout":
            challenge = self.challenges.pop(user, None)
            if challenge:
                challenge["active"] = False
                if challenge.get("task"): challenge["task"].cancel()
            # Lookup AFTER cancellation, under the same Store lock as any in-flight proof commit.
            listing = await self.offload(self.store.owner_listing, user)
            if not listing: return authored(REMOVED)
            okay = await self.remove_listing(listing["id"])
            return authored(REMOVED if okay else "Pop1 Live: locally removed. Some remote edits failed; bounded reconciliation queued.")
        listing = await self.offload(self.store.owner_listing, user)
        if name == "optin": return await self.begin_challenge(body, user, fields, listing)
        if name == "profile":
            if not listing: return authored("Pop1 Live: run /optin to prove your channel first.")
            await self.offload(self.store.update_profile, listing["id"], ign=fields.get("ign"),
                               timezone=fields.get("tz"), alerts_enabled=fields.get("alerts"))
            profile = await self.offload(self.store.profile, listing["id"])
            status = REJECTED if profile["review_status"] == "removed" else APPROVED if profile["review_status"] == "reviewed" else PENDING
            payload = authored(status + "\n" + message(f'IGN: {profile["ign"] or "not set"} · Timezone: {profile["timezone"]} · Go-live DMs: {"on" if profile["alerts_enabled"] else "off"}')["content"]
                               + "\nWould you pay $3/mo to be pinned at the top?")
            payload["components"] = [self.demand_button(listing["id"], user)]
            return payload
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
            payload = dict(text, flags=68) if isinstance(text, dict) else dict(message(text), flags=68)
            await self.edit_original(body, payload)
            user = (body.get("member", {}).get("user", {}) if body.get("guild_id") else body.get("user", {})).get("id")
            challenge = self.challenges.get(user)
            if challenge and challenge["interaction"]["id"] == body["id"] and not challenge.get("task"):
                challenge["task"] = self.spawn(self.check_challenge(challenge))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Discord follow-up: %s", type(exc).__name__)

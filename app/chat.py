"""Chat sources -> batched banner text -> %LOCALAPPDATA%\\LiveChatXR\\banner.txt.

The OpenXR layer (layer/livechat_xr_layer.cpp) watches banner.txt and shows each new write as a
head-locked banner for [banner] seconds. Comments arriving within one window share one banner.
"""
import asyncio
import json
import configparser
import logging
import os
import random
import re
import ssl
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LiveChatXR"
DEFAULTS = {
    "chat": {"tiktok": "", "twitch": "", "youtube": "", "platform": "twitch", "channel": "", "tiktok_sign_api_key": "",
             "discord_webhook": "", "follow_ask": "follow for the next sword-only run", "show_tag": "true",
             "platform_icons": "true", "viewer_count": "true"},
    "games": {"exes": "PopulationONE.exe"},
    "banner": {"seconds": "7", "up": "0.22", "distance": "1.0", "width": "0.62", "max_lines": "3"},
    "viewers": {"up": "0.36", "right": "0.45", "distance": "1.0", "width": "0.44"},  # read by the layer only
}
PLATFORMS = ("tiktok", "twitch", "youtube")
log = logging.getLogger("livechatxr")


def load_config(portable: bool = False) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    if portable:
        cp["games"]["exes"] = "LiveChatXR-QA-NotAGame.exe"
    cp.read(DIR / "config.ini", encoding="utf-8")
    c = cp["chat"]
    if c["channel"].strip() and not channels(cp):  # pre-0.2 config: one platform + channel -> per-platform keys
        c["tiktok" if c["platform"].lower() == "tiktok" else "twitch"] = c["channel"].strip()
        c["channel"] = ""
    return cp


def channels(cp: configparser.ConfigParser) -> dict[str, str]:
    """{platform: channel} for each platform set in [chat]; all chats share one banner / Discord channel."""
    return {p: cp["chat"][p].strip() for p in PLATFORMS if cp["chat"].get(p, "").strip()}


def save_config(cp: configparser.ConfigParser) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "config.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        cp.write(f)
    os.replace(tmp, DIR / "config.ini")


GIFT = "🎁 "  # the layer draws lines starting with this in gold
ASK = "» "  # follow-ask lines take priority over gifts and comments
ICONS = {"tiktok": "🎵 ", "twitch": "🟣 ", "youtube": "▶️ "}  # optional platform marker at the start of each line


def _bare(line: str) -> str:
    return next((line[len(i):] for i in ICONS.values() if line.startswith(i)), line)


def batch(lines: list[str], max_lines: int = 3, show_tag: bool = False) -> str:
    """Merge queued lines into one banner: follow asks, gifts, then comments, up to max_lines."""
    lines = sorted(lines, key=lambda line: (not line.startswith(ASK), not _bare(line).startswith(GIFT)))
    shown = [line + "  - LiveChat XR" if show_tag and line.startswith(ASK) else line
             for line in lines[:max_lines]]
    extra = len(lines) - len(shown)
    return "\n".join(shown) + (f"\n+{extra} more" if extra else "")


def write_banner(text: str, name: str = "banner.txt") -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / (name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, DIR / name)  # atomic: the layer never reads a half-written file


def post_discord(webhook: str, text: str, *, on_error=None, on_retry=None) -> bool:
    """Post one batch to a Discord channel webhook. Discord's own app shows it as a notification
    (on Quest that includes pop-ups during games), which is the standalone/companion display path."""
    body = json.dumps({
        "content": text[:2000],
        "username": "LiveChat XR",
        "allowed_mentions": {"parse": []},  # chat text must never ping @everyone/@here/roles
        "flags": 4,  # SUPPRESS_EMBEDS: links typed by viewers never unfold into previews or images
    }).encode()
    req = urllib.request.Request(webhook, body, {"Content-Type": "application/json",
                                                "User-Agent": "LiveChatXR (github.com/DeliciousHouse/livechat-xr)"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:
        code = getattr(e, "code", 0)
        if isinstance(e, urllib.error.HTTPError) and code == 429 and on_retry:
            delay = 1.0
            try:
                delay = float(e.headers.get("Retry-After") or json.load(e).get("retry_after", 1))
            except (ValueError, TypeError, AttributeError, OSError):
                pass
            on_retry(max(0.01, delay))
        log.warning("discord post failed: %s", code or type(e).__name__)
        if on_error:
            on_error(code)
        return False


class DiscordQueue:
    """One ordered sender per webhook; HTTP never blocks chat collection.

    Entries count original chat lines, not fragments or service notifications.
    Only unsent entries older than 60 seconds may be collapsed.
    """
    def __init__(self, webhook, on_delivery=None):
        self.webhook, self.on_delivery = webhook, on_delivery
        self.pending, self.sent = deque(), deque()
        self.ready, self.idle = asyncio.Event(), asyncio.Event()
        self.idle.set()

    def report(self, **counts):
        if self.on_delivery:
            self.on_delivery(**counts)

    def put(self, lines, platforms=(), suffix="", *, count=True):
        now = time.monotonic()
        group = object()
        self.report(received=len(lines) if count else 0)
        for line, counted in [(line, int(count)) for line in lines] + ([(suffix, 0)] if suffix else []):
            # Split on physical lines, then UTF-16 boundaries (Discord counts emoji as two).
            pieces = []
            for physical in line.split("\n"):
                raw = physical.encode("utf-16-le")
                while raw:
                    end = min(len(raw), 4000)
                    if 0xD800 <= int.from_bytes(raw[end - 2:end], "little") <= 0xDBFF:
                        end -= 2
                    pieces.append(raw[:end].decode("utf-16-le"))
                    raw = raw[end:]
                if not physical:
                    pieces.append("")
            for i, piece in enumerate(pieces):
                self.pending.append((now, piece, counted if i == len(pieces) - 1 else 0, tuple(platforms), group))
        if self.pending:
            self.idle.clear()
            self.ready.set()

    async def join(self):
        await self.idle.wait()

    async def run(self):
        while True:
            await self.ready.wait()
            now = time.monotonic()
            while self.sent and now - self.sent[0] >= 60:
                self.sent.popleft()
            if len(self.sent) >= 25:
                await asyncio.sleep(max(0.01, 60 - (now - self.sent[0])))
                continue
            dropped = 0
            while self.pending and now - self.pending[0][0] > 60:
                dropped += self.pending.popleft()[2]
            if dropped:
                self.pending.appendleft((now, f"+{dropped} earlier messages", 0, (), object()))
                self.report(dropped_backlog=dropped)
            if not self.pending:
                self.ready.clear()
                self.idle.set()
                continue
            entries, size = [], 0
            for entry in self.pending:
                length = len(entry[1].encode("utf-16-le")) // 2
                if entries and (entry[4] is not entries[0][4] or len(entries) == 5 or size + 1 + length > 2000):
                    break
                entries.append(entry)
                size += length + int(len(entries) > 1)
            text = "\n".join(entry[1] for entry in entries)
            errors, retry = [], []
            self.sent.append(time.monotonic())  # failed attempts also consume the local budget
            ok = await asyncio.to_thread(post_discord, self.webhook, text,
                                         on_error=errors.append, on_retry=retry.append)
            platforms = sorted({p for entry in entries for p in entry[3]})
            self.report(ok=ok is not False, error=errors[0] if errors else -1, platforms=platforms,
                        posted=sum(entry[2] for entry in entries) if ok is not False else 0)
            if ok is False:
                await asyncio.sleep(retry[0] if retry else 5)
                continue
            for _ in entries:
                self.pending.popleft()
            if not self.pending:
                self.ready.clear()
                self.idle.set()


# ---------------------------------------------------------------- Twitch (anonymous IRC, no key needed)
_LINE = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<login>[^!\s]+)\S* (?P<cmd>PRIVMSG|USERNOTICE) #\S+(?: :(?P<msg>.*))?$")
_UNESCAPE = {"s": " ", ":": ";", "\\": "\\", "r": "", "n": ""}
_SUPPORT = {"sub", "resub", "subgift", "submysterygift", "giftpaidupgrade", "anongiftpaidupgrade", "primepaidupgrade"}


def _tags(raw: str) -> dict[str, str]:
    out = {}
    for kv in raw.split(";") if raw else ():
        k, _, v = kv.partition("=")
        out[k] = re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m[1], m[1]), v)
    return out


def twitch_line(line: str, channel: str) -> str | None:
    """IRC line -> banner line: chat, cheers (bits) and subs/gift subs. None for everything else."""
    m = _LINE.match(line)
    if not m:
        return None
    tags, msg = _tags(m["tags"] or ""), m["msg"] or ""
    name = tags.get("display-name") or m["login"]
    if m["cmd"] == "PRIVMSG":
        if tags.get("bits"):
            return f"{GIFT}{name} cheered {tags['bits']} bits" + (f": {msg}" if msg else "")
        return None if m["login"] == channel else f"{name}: {msg}"
    kind = tags.get("msg-id", "")
    if kind not in _SUPPORT or (kind == "subgift" and "msg-param-community-gift-id" in tags):
        return None  # individual subs of a mass gift: the submysterygift line already covers them
    return GIFT + (tags.get("system-msg") or f"{name}: {kind}") + (f" — {msg}" if msg else "")

_twitch_token = ("", 0.0)
_twitch_lock = threading.Lock()


def _twitch_access() -> str:
    global _twitch_token
    with _twitch_lock:
        if _twitch_token[1] <= time.time():
            body = urllib.parse.urlencode({"client_id": os.environ["TWITCH_CLIENT_ID"],
                                           "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
                                           "grant_type": "client_credentials"}).encode()
            with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", body, timeout=10) as r:
                data = json.load(r)
            _twitch_token = (data["access_token"], time.time() + int(data["expires_in"]) - 60)
        return _twitch_token[0]


def resolve_identity(platform: str, handle: str, identity: dict | None = None) -> dict | None:
    """Best-effort permanent ID + current handle. Callers retain a known ID on lookup failures."""
    identity = identity or {}
    if platform == "twitch":
        if not (os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")):
            return None
        q = {"id": identity["id"]} if identity.get("id") else {"login": handle.lstrip("@#")}
        req = urllib.request.Request("https://api.twitch.tv/helix/users?" + urllib.parse.urlencode(q),
                                     headers={"Client-ID": os.environ["TWITCH_CLIENT_ID"],
                                              "Authorization": "Bearer " + _twitch_access()})
        with urllib.request.urlopen(req, timeout=10) as r:
            users = json.load(r).get("data", [])
        return {"id": users[0]["id"], "handle": users[0]["login"]} if users else None
    if platform == "youtube":
        path = "channel/" + identity["id"] if identity.get("id") else handle
        text = _yt_get("https://www.youtube.com/" + path)
        m = re.search(r'"channelMetadataRenderer"\s*:\s*', text)
        if m:
            meta, _ = json.JSONDecoder().raw_decode(text[m.end():])
            cid = meta.get("externalId", "")
            current = urllib.parse.urlparse(meta.get("vanityChannelUrl", "")).path.strip("/")
            if re.fullmatch(r"UC[\w-]{22}", cid):
                return {"id": cid, "handle": current if current.startswith("@") else handle}
    if platform == "tiktok":
        text = _yt_get("https://www.tiktok.com/" + handle)
        m = re.search(r'<script\b[^>]*\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', text, re.S)
        if m:
            owner = json.loads(m[1]).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {}).get("user", {})
            if owner.get("id") and owner.get("uniqueId", "").lower() == handle.lstrip("@").lower():
                return {"id": str(owner["id"]), "sec_uid": owner.get("secUid", ""), "handle": "@" + owner["uniqueId"]}
    return None


async def refresh_identity(platform, handle, identity, on_identity):
    try:
        found = await asyncio.to_thread(resolve_identity, platform, handle, identity)
        if found:
            identity.update(found)
            on_identity(found)
            return found["handle"]
    except Exception as e:
        log.warning("%s identity lookup: %s", platform, type(e).__name__)
    return handle


async def twitch(channel: str, emit, status, tally=None, *, identity=None, on_identity=None) -> None:
    channel = channel.lower().lstrip("#")
    identity = identity if identity is not None else {}
    while True:
        w = None
        joined = not on_identity
        try:
            if on_identity:
                channel = await refresh_identity("twitch", channel, identity, on_identity)
            r, w = await asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=ssl.create_default_context())
            w.write(f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nNICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{channel}\r\n".encode())
            await w.drain()
            if joined:
                status(f"Twitch: connected to #{channel}")
            while True:
                try:
                    raw = await asyncio.wait_for(r.readline(), 360 if joined else 20)  # Twitch PINGs every ~5 min
                except TimeoutError:
                    if joined:
                        raise
                    status(f"Twitch: can't find {channel}")
                    await asyncio.sleep(60)
                    break
                if not raw:
                    raise ConnectionError("connection closed")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if on_identity and " ROOMSTATE " in line:
                    room_id = _tags(line.split(" ", 1)[0].lstrip("@")).get("room-id")
                    if room_id:
                        if identity.get("id") and identity["id"] != room_id:
                            status(f"Twitch: can't find {channel}")
                            await asyncio.sleep(60)
                            break  # old login now belongs to somebody else; never relay their chat
                        identity.update(id=room_id, handle=channel)
                        on_identity(dict(identity))
                    joined = True
                    status(f"Twitch: connected to #{channel}")
                elif line.startswith("PING"):
                    w.write(f"PONG{line[4:]}\r\n".encode())
                    await w.drain()
                    if on_identity:
                        current = await refresh_identity("twitch", channel, identity, on_identity)
                        if current != channel:
                            channel = current
                            break  # reconnect IRC using the login resolved from the permanent ID
                elif joined and (out := twitch_line(line, channel)):
                    parsed = _LINE.match(line)
                    if tally and parsed:
                        if parsed["cmd"] == "USERNOTICE" or _tags(parsed["tags"] or "").get("bits"):
                            tally.gifts += 1
                        else:
                            tally.comment(parsed["login"].lower())
                    emit(out)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status(f"Twitch: reconnecting ({getattr(e, 'code', 0) or e.__class__.__name__})")
            log.warning("twitch: %s", type(e).__name__)
            await asyncio.sleep(10)
        finally:
            if w:
                w.close()
                await w.wait_closed()


# ---------------------------------------------------------------- TikTok (unofficial; TikTokLive library)
def _who(u) -> tuple[str, str]:
    """TikTok user -> (@handle, display name)."""
    handle = (getattr(u, "unique_id", None) or getattr(u, "display_id", "") or "") if u else ""
    return handle, (getattr(u, "nickname", "") if u else "") or handle or "?"


def _following(u) -> bool | None:
    """Does this TikTok viewer follow the streamer? follow_info.follow_status 0 = no, 1 = follows, 2 = friends;
    None when TikTok didn't send it."""
    status = getattr(getattr(u, "follow_info", None), "follow_status", None)
    return status >= 1 if isinstance(status, int) else None


def session_summary(row: dict) -> str:
    minutes = max(0, int((datetime.fromisoformat(row["end"]) -
                          datetime.fromisoformat(row["start"])).total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    parts = [f"Stream ended {row['channel']}", f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"]
    for key, label in (("comments", "{} comments"), ("distinct_chatters", "{} distinct chatters"),
                       ("gifts", "{} gifts/support events"), ("peak_viewers", "peak {} viewers"),
                       ("new_follows", "{} new follows"), ("asks_shown", "{} follow asks shown")):
        if row.get(key) is not None:
            parts.append(label.format(row[key]))
    return " - ".join(parts)


class Tally:
    """Connection-local counts; chatter identifiers never leave memory."""
    def __init__(self, platform: str, channel: str):
        self.platform, self.channel = platform, "@" + channel.lstrip("@#")
        self.start = datetime.now(timezone.utc)
        self.comments, self.gifts = 0, 0
        self.chatters, self.followers = set(), set()
        self.peak: int | None = None

    def comment(self, viewer: str) -> None:
        self.comments += 1
        self.chatters.add(viewer)

    def row(self) -> dict:
        return {"start": self.start.isoformat(timespec="seconds"),
                "end": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "platform": self.platform, "channel": self.channel, "comments": self.comments,
                "distinct_chatters": len(self.chatters), "gifts": self.gifts, "peak_viewers": self.peak,
                "new_follows": len(self.followers) if self.platform == "tiktok" else None}


class Session:
    """One TikTok LIVE connection: the follow ask plus the new-follower tally written to sessions.jsonl.

    A viewer's second message (or a later one, if the global cooldown blocked it) gets `ask` once, unless they
    are a known follower; unknown follow status is asked. At most one ask per `every` seconds across all viewers."""

    def __init__(self, ask: str, every: float = 90):
        self.ask, self.every = ask.strip(), every
        self.start = datetime.now(timezone.utc)
        self.said: dict[str, int] = {}
        self.done: set[str] = set()  # asked already or known follower
        self.followers: set[str] = set()
        self.last: float | None = None
        self.asks = 0
        self.peak: int | None = None

    def comment(self, viewer: str, following: bool | None, now: float) -> str | None:
        """Count a chat message; return the banner line to show, or None."""
        self.said[viewer] = self.said.get(viewer, 0) + 1
        if following:
            self.done.add(viewer)
        if (not self.ask or self.said[viewer] < 2 or viewer in self.done
                or (self.last is not None and now - self.last < self.every)):
            return None
        self.done.add(viewer)
        self.last, self.asks = now, self.asks + 1
        return ASK + self.ask

    def follow(self, viewer: str) -> None:
        self.followers.add(viewer)  # distinct viewers: a re-follow in the same stream counts once
        self.done.add(viewer)

    def viewers(self, n: int) -> None:
        self.peak = max(self.peak or 0, n)

    def save(self, channel: str, webhook: str = "") -> dict:
        row = {"start": self.start.isoformat(timespec="seconds"),
               "end": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "channel": "@" + channel, "peak_viewers": self.peak,
               "new_follows": len(self.followers), "asks_shown": self.asks}
        DIR.mkdir(parents=True, exist_ok=True)
        with open(DIR / "sessions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        log.info("tiktok session: %s", row)
        if webhook:
            text = session_summary(row)
            for notify in (lambda: write_banner(text), lambda: post_discord(webhook, text)):
                try:
                    notify()
                except Exception:
                    log.warning("session notification failed")
        return row


async def tiktok(user: str, emit, status, sign_api_key: str = "", follow_ask: str | None = None,
                 tally: Tally | None = None, webhook: str = "", *, identity=None, on_identity=None,
                 on_viewers=None, on_owner_comment=None) -> None:
    """follow_ask=None: plain chat (hosted relay). A string turns on a Session per connection: the follow ask
    (empty string = no ask) and a sessions.jsonl row when the connection ends.
    on_owner_comment(handle, text) is synchronous and must return quickly without blocking.
    Once per owner comment, before suppressing it, receives the nonempty lowercase unique/display
    handle without @ and the unchanged comment; nicknames never establish ownership.
    Ordinary callback exceptions log a fixed message and chat continues; cancellation propagates."""
    from TikTokLive import TikTokLiveClient
    from TikTokLive.client.errors import UserNotFoundError
    from TikTokLive.events import CommentEvent, ConnectEvent, FollowEvent, GiftEvent, RoomUserSeqEvent

    user = user.lstrip("@").lower()  # TikTok handles are case-insensitive, but the LIVE lookup only matches lowercase
    identity = identity if identity is not None else {}
    missing = 0
    if sign_api_key:
        os.environ["SIGN_API_KEY"] = sign_api_key  # read by TikTokLive's signer
    while True:
        client = TikTokLiveClient(unique_id=user)
        session: Session | None = None

        @client.on(ConnectEvent)
        async def _(e: ConnectEvent):
            nonlocal missing, session
            missing = 0
            if on_identity:
                room = getattr(client, "room_info", None)
                if not room:
                    try:
                        room = await client.web.fetch_room_info()
                    except Exception as error:
                        log.warning("tiktok room identity lookup: %s", type(error).__name__)
                owner = room.get("owner", {}) if isinstance(room, dict) else {}
                if owner.get("id"):
                    found = {"id": str(owner["id"]), "sec_uid": owner.get("sec_uid", ""), "handle": "@" + user}
                    identity.update(found)
                    on_identity(found)
                else:
                    await refresh_identity("tiktok", "@" + user, identity, on_identity)
            if follow_ask is not None:
                session = Session(follow_ask)
            status(f"TikTok: connected to @{user}")

        @client.on(CommentEvent)
        async def _(e: CommentEvent):
            handle, name = _who(e.user)
            owner = handle.lstrip("@").lower()
            if owner and owner == user:  # skip the streamer's own messages
                if on_owner_comment is not None:
                    try:
                        on_owner_comment(owner, e.comment)
                    except Exception:
                        log.warning("tiktok owner comment callback failed")
            else:
                if tally:
                    tally.comment(handle.lower() or name)
                emit(f"{name}: {e.comment}")
                if session and (ask := session.comment(handle.lower() or name, _following(e.user), time.monotonic())):
                    emit(ask)

        @client.on(FollowEvent)
        async def _(e: FollowEvent):
            if tally:
                handle, name = _who(e.user)
                tally.followers.add(handle.lower() or name)
            if session:
                handle, name = _who(e.user)
                session.follow(handle.lower() or name)

        @client.on(RoomUserSeqEvent)
        async def _(e: RoomUserSeqEvent):
            if tally and isinstance(getattr(e, "total", None), int):
                tally.peak = max(tally.peak or 0, e.total)
            if session and isinstance(getattr(e, "total", None), int):
                session.viewers(e.total)  # current viewer count; peak_viewers is its max
            if on_viewers and isinstance(getattr(e, "total", None), int):
                on_viewers(e.total)

        @client.on(GiftEvent)
        async def _(e: GiftEvent):
            if e.streaking or not e.gift:  # a combo sends many events; show only the final total
                return
            n = e.repeat_count or 1
            if tally:
                tally.gifts += 1
            emit(f"{GIFT}{_who(e.user)[1]} sent {e.gift.name}" + (f" x{n}" if n > 1 else ""))

        retry = 60
        try:
            await client.connect(process_connect_events=False)  # returns when the stream ends
            missing = 0
            status(f"TikTok: @{user} went offline, waiting")
        except asyncio.CancelledError:
            if client.connected:
                await client.disconnect()
            raise
        except Exception as e:
            missing = missing + 1 if isinstance(e, UserNotFoundError) else 0
            if missing:
                # "Not found" also means "never gone LIVE", so a first-time streamer must be picked up within a minute.
                status(f"TikTok: can't find @{user}. Check the spelling or permission to go LIVE; retrying in 1 minute.")
            offline = "offline" in type(e).__name__.lower()
            if not missing:
                status(f"TikTok: waiting for @{user} to go live" if offline else f"TikTok: retrying ({getattr(e, 'code', 0) or type(e).__name__})")
            if not offline:
                log.warning("tiktok: %s", type(e).__name__)
        finally:
            if on_viewers:
                on_viewers(None)
            if session:  # stream ended, app stopped or connection dropped; a reconnect starts a new row
                try:
                    await asyncio.to_thread(session.save, user, webhook)
                except Exception as e:
                    log.warning("sessions.jsonl: %r", e)
        await asyncio.sleep(retry)


# ---------------------------------------------------------------- YouTube (unofficial; the live page's own chat endpoint, no key, no login)
_YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9"}
_YT_PAID = {"liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"}
_YT_MEMBER = {"liveChatMembershipItemRenderer", "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer"}


def _yt_text(obj) -> str:
    """YouTube text: {"simpleText": s} or {"runs": [{"text": s} | {"emoji": {...}}]}; emoji as the character or :shortcut:."""
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    out = []
    for r in obj.get("runs", ()):
        if "emoji" in r:
            e = r["emoji"]
            out.append((e.get("shortcuts") or [""])[0] if e.get("isCustomEmoji") else e.get("emojiId", ""))
        else:
            out.append(r.get("text", ""))
    return "".join(out)


def youtube_line(item: dict, owner: str = "") -> str | None:
    """One addChatItemAction item -> banner line: chat, Super Chats / stickers and memberships (GIFT). None otherwise."""
    for kind, r in item.items():
        header = r.get("header", {}).get("liveChatSponsorshipsHeaderRenderer", {})
        name = _yt_text(r.get("authorName") or header.get("authorName")) or "?"
        msg = _yt_text(r.get("message"))
        if kind == "liveChatTextMessageRenderer":
            return None if owner and r.get("authorExternalChannelId") == owner else f"{name}: {msg}"
        if kind in _YT_PAID:
            return f"{GIFT}{name} sent {_yt_text(r.get('purchaseAmountText')) or 'a Super Chat'}" + (f": {msg}" if msg else "")
        if kind in _YT_MEMBER:
            text =_yt_text(header.get("primaryText")) or _yt_text(r.get("headerSubtext")) or _yt_text(r.get("headerPrimaryText"))
            return GIFT + (text if text.lower().startswith(name.lower()) else f"{name}: {text or 'new member'}")
    return None


def yt_page(html_text: str) -> dict | None:
    """Live page -> {key, version, continuation, owner}, or None when the channel is not live right now."""
    # An ended broadcast's /live page still says "isLive":true but "isLiveNow":false; trust isLiveNow when present,
    # or the relay re-attaches to the dead chat and misses the next stream (2026-10-16).
    now = re.search(r'"isLiveNow":(true|false)', html_text)
    if (now[1] != "true") if now else not re.search(r'"isLive":true', html_text):
        return None
    # The channel-ID /live page has no isLiveNow; after a stream it shows a scheduled placeholder that still says
    # "isLive":true and has a chat, but its player reports LIVE_STREAM_OFFLINE and isUpcoming (2026-10-16).
    playable = re.search(r'"playabilityStatus":\{"status":"(\w+)"', html_text)
    if (playable and playable[1] != "OK") or re.search(r'"isUpcoming":true', html_text):
        return None
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html_text)
    cont = re.search(r'"liveChatRenderer":\{.{0,600}?"continuation":"([^"]+)"', html_text, re.S)
    if not (key and cont):
        return None
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html_text)
    owner = re.search(r'"channelId":"(UC[\w-]{22})"', html_text)
    return {"key": key[1], "version": ver[1] if ver else "2.20240101.00.00", "continuation": cont[1], "owner": owner[1] if owner else ""}


def yt_items(lc: dict):
    """(id, item) for every chat item added in one get_live_chat response."""
    for a in lc.get("actions", ()):
        item = a.get("addChatItemAction", {}).get("item", {})
        for r in item.values():
            if r.get("id"):
                yield r["id"], item


def yt_next(lc: dict) -> tuple[str, float] | None:
    """(next continuation, seconds to wait) from a response; None when the stream has ended (replay continuation)."""
    for c in lc.get("continuations", ()):
        for kind, d in c.items():
            if kind == "liveChatReplayContinuationData":
                return None
            if d.get("continuation"):
                return d["continuation"], min(max(int(d.get("timeoutMs", 5000)) / 1000, 1), 30)
    return None


def yt_all_chat(lc: dict) -> str | None:
    """The "Live chat" (all messages) continuation when the current view is "Top chat", which hides messages."""
    sel = lc.get("header", {}).get("liveChatHeaderRenderer", {}).get("viewSelector", {}).get("sortFilterSubMenuRenderer", {})
    for s in sel.get("subMenuItems", ()):
        if not s.get("selected") and s.get("title") == "Live chat":
            return s.get("continuation", {}).get("reloadContinuationData", {}).get("continuation")
    return None


def _yt_get(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_YT_HEADERS), timeout=15) as r:
        return r.read(3_000_000).decode("utf-8", "replace")


def _yt_chat(info: dict, continuation: str) -> dict:
    body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": info["version"], "hl": "en"}},
                       "continuation": continuation}).encode()
    req = urllib.request.Request(f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={info['key']}&prettyPrint=false",
                                 body, {**_YT_HEADERS, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("continuationContents", {}).get("liveChatContinuation", {})


async def youtube(handle: str, emit, status, tally=None, *, identity=None, on_identity=None) -> None:
    """Chat of the channel's current live stream, read the way the live page itself does (no API key, no login).
    The backlog YouTube sends on (re)connect is skipped so the banner never replays old chat."""
    handle = handle.strip().lstrip("@")
    identity = identity if identity is not None else {}
    while True:
        try:
            if on_identity:
                handle = (await refresh_identity("youtube", "@" + handle, identity, on_identity)).lstrip("@")
            path = "channel/" + identity["id"] if identity and identity.get("id") else "@" + handle
            info = yt_page(await asyncio.to_thread(_yt_get, f"https://www.youtube.com/{path}/live"))
            if not info:
                status(f"YouTube: waiting for @{handle} to go live")
                await asyncio.sleep(60)
                continue
            status(f"YouTube: connected to @{handle}")
            cont, seen, backlog, switched = info["continuation"], {}, True, False
            while True:
                lc = await asyncio.to_thread(_yt_chat, info, cont)
                if not switched and (all_chat := yt_all_chat(lc)):
                    cont, switched = all_chat, True  # the page's continuation is the filtered "Top chat"; switch once
                    continue
                for item_id, item in yt_items(lc):
                    if item_id in seen:
                        continue
                    seen[item_id] = None
                    if not backlog and (line := youtube_line(item, info["owner"])):
                        if tally:
                            if next(iter(item)) != "liveChatTextMessageRenderer":
                                tally.gifts += 1
                            else:
                                renderer = next(iter(item.values()))
                                tally.comment(renderer.get("authorExternalChannelId") or _yt_text(renderer.get("authorName")))
                        emit(line)
                while len(seen) > 4000:  # ids only matter across one response boundary
                    del seen[next(iter(seen))]
                backlog = False
                nxt = yt_next(lc)
                if nxt is None:
                    status(f"YouTube: @{handle} went offline, waiting")
                    await asyncio.sleep(60)
                    break
                cont, wait = nxt
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            code = getattr(e, "code", 0)
            status(f"YouTube: can't find @{handle}" if code == 404 else f"YouTube: retrying ({code or e.__class__.__name__})")
            log.warning("youtube: %r", e)
            await asyncio.sleep(30)


# ---------------------------------------------------------------- viewer counts (PC banner footer)
VIEWERS_EVERY = 5  # seconds between viewers.txt writes; the layer hides it 20 s after the last one
_TWITCH_WEB_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # twitch.tv's own public web client id; no login, no secret


def viewer_count(platform: str, channel: str) -> int | None:
    """Current viewers of a live Twitch / YouTube stream; None when offline. TikTok pushes its count instead."""
    channel = channel.strip().lstrip("@#")
    if platform == "twitch":
        body = json.dumps({"query": "query($l:String!){user(login:$l){stream{viewersCount}}}",
                           "variables": {"l": channel.lower()}}).encode()
        req = urllib.request.Request("https://gql.twitch.tv/gql", body,
                                     {"Client-Id": _TWITCH_WEB_CLIENT, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            stream = ((json.load(r).get("data") or {}).get("user") or {}).get("stream")
        return stream["viewersCount"] if stream else None
    if platform == "youtube":
        page = _yt_get(f"https://www.youtube.com/@{channel}/live")
        m = re.search(r'"originalViewCount":"(\d+)"', page)
        return int(m[1]) if m and re.search(r'"isLive(?:Now)?":true', page) else None
    return None


def viewer_lines(counts: dict[str, int | None]) -> str:
    """viewers.txt for the layer's corner box: 'tiktok 4\\ntwitch 1' for live platforms; '' (hidden) when none is."""
    return "\n".join(f"{p} {counts[p]}" for p in PLATFORMS if counts.get(p) is not None)


# ---------------------------------------------------------------- relay: chat source -> one batch per window
async def relay(platform: str, channel: str, on_batch, status, seconds: float = 7, max_lines: int = 3,
                sign_api_key: str = "", channels: dict[str, str] | None = None, follow_ask: str | None = None,
                show_tag: bool = False, on_connect=None, *, on_session=None, webhook: str = "",
                identities=None, on_identity=None, on_source_status=None, on_batch_sources=None,
                on_lines=None, platform_icons: bool = False, on_viewers=None) -> None:
    """Run the chat source(s) and call on_batch(text) once per window that had comments. Used by the PC app
    (banner + optional Discord) and by the hosted server (Discord only).

    channels={"tiktok": "@a", "twitch": "b", "youtube": "@c"} reads all of them into the same batches (gifts from any
    sort before comments); status() then gets the per-platform lines joined with " · ". Without it, platform/channel is one source.
    follow_ask goes to tiktok(): the PC app sets it from config, the hosted relay leaves it None.
    show_tag tags only ASK lines; on_connect fires on each source's connected status transition.
    platform_icons starts each chat line with its platform's ICONS emoji (follow asks stay unmarked).
    on_viewers(count) gets TikTok's live viewer count (None when the connection ends)."""
    queue: list[str] = []
    queued_platforms: set[str] = set()
    channels = channels or {platform: channel}
    statuses: dict[str, str] = {}

    def source(p: str, c: str):
        p, c = p.lower(), c.strip()
        tally = Tally(p, c) if on_session else None
        active = False
        def st(s: str) -> None:
            nonlocal active
            new_connection = s.partition(":")[2].startswith(" connected to ") and not active
            if new_connection:
                active = True
                if tally:
                    tally.__init__(p, c)
            elif "went offline" in s and active:
                active = False  # consume before callback: failures never duplicate a summary
                if tally and on_session:
                    try:
                        on_session(tally.row())
                    except Exception:
                        log.warning("stream stats failed")
            elif ": retrying" in s or ": reconnecting" in s:
                active = False  # a transport error is not proof that the stream ended
            statuses[p] = s
            if on_source_status:
                on_source_status(p, s)
            status(" · ".join(statuses[k] for k in sorted(statuses)))  # "TikTok: … · Twitch: … · YouTube: …"
            if new_connection and on_connect:
                try:
                    on_connect()
                except Exception:
                    log.warning("connection notification failed")
        kwargs: dict = {"tally": tally} if tally else {}
        if on_identity:
            kwargs.update(identity=(identities or {}).get(p, {}).copy(), on_identity=lambda found: on_identity(p, found))
        def emit(line):
            # One chat message is one line: an embedded newline must not forge extra banner lines or a gold gift line.
            line = " ".join(line.splitlines())
            queue.append(ICONS[p] + line if platform_icons and p in ICONS and not line.startswith(ASK) else line)
            queued_platforms.add(p)
        if p == "tiktok":
            if webhook:
                kwargs["webhook"] = webhook
            if on_viewers:
                kwargs["on_viewers"] = on_viewers
            return tiktok(c, emit, st, sign_api_key, follow_ask, **kwargs)
        return youtube(c, emit, st, **kwargs) if p == "youtube" else twitch(c, emit, st, **kwargs)

    srcs = [asyncio.ensure_future(source(p, c)) for p, c in channels.items()]
    try:
        while not any(s.done() for s in srcs):  # sources only finish on error or cancel; one failing restarts all
            await asyncio.sleep(seconds)
            if queue:
                lines = queue[:]
                queue.clear()
                if on_lines:
                    on_lines([line + "  - LiveChat XR" if show_tag and line.startswith(ASK) else line
                              for line in lines], sorted(queued_platforms))
                text = batch(lines, max_lines, show_tag)
                if on_batch_sources:
                    on_batch_sources(text, sorted(queued_platforms))
                else:
                    on_batch(text)
                queued_platforms.clear()
        for s in srcs:
            if s.done():
                s.result()
    finally:
        if queue and on_lines:
            on_lines(queue[:], sorted(queued_platforms))
        for s in srcs:
            s.cancel()
        await asyncio.gather(*srcs, return_exceptions=True)


# ---------------------------------------------------------------- runner (background thread + event loop)
class Runner:
    def __init__(self):
        self.status = "Stopped"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None

    def _set_status(self, s: str) -> None:
        if s != self.status:
            log.info(s)
        self.status = s

    async def _main(self, cfg: configparser.ConfigParser) -> None:
        webhook = cfg["chat"].get("discord_webhook", "").strip()
        show_tag = cfg.getboolean("chat", "show_tag", fallback=True)
        counts: dict[str, int | None] = {}

        def on_batch(text: str) -> None:
            write_banner(text)

        async def poll(p: str, c: str) -> None:
            while True:
                try:
                    counts[p] = await asyncio.to_thread(viewer_count, p, c)
                except Exception as e:
                    counts[p] = None  # an unknown count is left out, never shown as an error
                    log.warning("%s viewers: %s", p, type(e).__name__)
                await asyncio.sleep(30)

        async def keep_fresh() -> None:  # the layer hides viewers.txt 20 s after its last write
            while True:
                write_banner(viewer_lines(counts), "viewers.txt")
                await asyncio.sleep(VIEWERS_EVERY)

        tasks = []
        viewers_on = cfg.getboolean("chat", "viewer_count", fallback=True)
        if viewers_on:
            tasks =[poll(p, c) for p, c in channels(cfg).items() if p != "tiktok"] + [keep_fresh()]
        sender = DiscordQueue(webhook) if webhook else None
        tasks += [sender.run()] if sender else []
        tasks = [asyncio.create_task(t) for t in tasks]
        try:
            await relay("", "", on_batch, self._set_status,
                    cfg.getfloat("banner", "seconds", fallback=7), cfg.getint("banner", "max_lines", fallback=3),
                    cfg["chat"].get("tiktok_sign_api_key", ""), channels=channels(cfg),
                    follow_ask=cfg["chat"].get("follow_ask", ""), show_tag=show_tag,
                    on_connect=(lambda: write_banner("LiveChat XR - livechat.deliciouswines.org")) if show_tag else None,
                    webhook=webhook, on_lines=sender.put if sender else None,
                    platform_icons=cfg.getboolean("chat", "platform_icons", fallback=True),
                    on_viewers=(lambda n: counts.__setitem__("tiktok", n)) if viewers_on else None)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if viewers_on:
                write_banner("", "viewers.txt")  # empty hides the corner box at once

    def start(self, cfg: configparser.ConfigParser) -> None:
        self.stop()
        if not channels(cfg):
            self._set_status("Not set up: open Settings and enter your TikTok handle or Twitch channel")
            return
        self._set_status("Connecting…")
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            self._task = self._loop.create_task(self._main(cfg))
            ready.set()
            try:
                self._loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._set_status(f"Stopped: {e}")
                log.exception("runner crashed")
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True, name="chat")
        self._thread.start()
        ready.wait()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and self._loop and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)
            self._thread.join(timeout=10)
        self._thread = None
        self._set_status("Stopped")


# ---------------------------------------------------------------- headless (no tray, no overlay)
# For a standalone headset: run this in Termux on the Quest (or on any phone/Mac/Linux box) and chat
# goes to your Discord channel, which the Quest's Discord app pops up in-game. No PC needed.
#   python chat.py tiktok <handle> <discord webhook URL>      (or: twitch <channel> <webhook>, youtube <handle> <webhook>)
# With no arguments it uses ~/LiveChatXR/config.ini (or %LOCALAPPDATA% on Windows).
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config()
    if len(sys.argv) == 4 and sys.argv[1] in PLATFORMS:
        for p in PLATFORMS:
            cfg["chat"][p] = ""
        cfg["chat"][sys.argv[1]], cfg["chat"]["discord_webhook"] = sys.argv[2:]
    elif len(sys.argv) != 1:
        sys.exit("usage: python chat.py [tiktok|twitch|youtube <channel> <discord webhook URL>]")
    if not channels(cfg):
        sys.exit("no channel set: pass  tiktok|twitch|youtube <channel> <webhook>  (or set [chat] tiktok / twitch / youtube in config.ini)")
    try:
        asyncio.run(Runner()._main(cfg))
    except KeyboardInterrupt:
        pass

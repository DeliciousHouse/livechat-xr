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
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LiveChatXR"
DEFAULTS = {
    "chat": {"tiktok": "", "twitch": "", "youtube": "", "platform": "twitch", "channel": "", "tiktok_sign_api_key": "",
             "discord_webhook": "", "follow_ask": "follow for the next sword-only run", "show_tag": "true"},
    "games": {"exes": "PopulationONE.exe"},
    "banner": {"seconds": "7", "up": "0.22", "distance": "1.0", "width": "0.62", "max_lines": "3"},
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


def batch(lines: list[str], max_lines: int = 3, show_tag: bool = False) -> str:
    """Merge queued lines into one banner: follow asks, gifts, then comments, up to max_lines."""
    lines = sorted(lines, key=lambda line: (not line.startswith(ASK), not line.startswith(GIFT)))
    shown = [line + "  - LiveChat XR" if show_tag and line.startswith(ASK) else line
             for line in lines[:max_lines]]
    extra = len(lines) - len(shown)
    return "\n".join(shown) + (f"\n+{extra} more" if extra else "")


def write_banner(text: str) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "banner.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, DIR / "banner.txt")  # atomic: the layer never reads a half-written file


def post_discord(webhook: str, text: str) -> bool:
    """Post one batch to a Discord channel webhook. Discord's own app shows it as a notification
    (on Quest that includes pop-ups during games), which is the standalone/companion display path."""
    body = json.dumps({
        "content": text[:2000],
        "username": "LiveChat XR",
        "allowed_mentions": {"parse": []},  # chat text must never ping @everyone/@here/roles
    }).encode()
    req = urllib.request.Request(webhook, body, {"Content-Type": "application/json",
                                                "User-Agent": "LiveChatXR (github.com/DeliciousHouse/livechat-xr)"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:  # 429 = Discord rate limit (30/min/channel); drop rather than pile up
        log.warning("discord post failed: %s", e)
        return False


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

async def twitch(channel: str, emit, status) -> None:
    channel = channel.lower().lstrip("#")
    while True:
        try:
            r, w = await asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=ssl.create_default_context())
            w.write(f"CAP REQ :twitch.tv/tags\r\nNICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{channel}\r\n".encode())
            await w.drain()
            status(f"Twitch: connected to #{channel}")
            while True:
                raw = await asyncio.wait_for(r.readline(), 360)  # Twitch PINGs every ~5 min
                if not raw:
                    raise ConnectionError("connection closed")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("PING"):
                    w.write(f"PONG{line[4:]}\r\n".encode())
                    await w.drain()
                elif out := twitch_line(line, channel):
                    emit(out)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status(f"Twitch: reconnecting ({e.__class__.__name__})")
            log.warning("twitch: %r", e)
            await asyncio.sleep(10)


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

    def save(self, channel: str) -> dict:
        row = {"start": self.start.isoformat(timespec="seconds"),
               "end": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "channel": "@" + channel, "peak_viewers": self.peak,
               "new_follows": len(self.followers), "asks_shown": self.asks}
        DIR.mkdir(parents=True, exist_ok=True)
        with open(DIR / "sessions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        log.info("tiktok session: %s", row)
        return row


async def tiktok(user: str, emit, status, sign_api_key: str = "", follow_ask: str | None = None) -> None:
    """follow_ask=None: plain chat (hosted relay). A string turns on a Session per connection: the follow ask
    (empty string = no ask) and a sessions.jsonl row when the connection ends."""
    from TikTokLive import TikTokLiveClient
    from TikTokLive.client.errors import UserNotFoundError
    from TikTokLive.events import CommentEvent, ConnectEvent, FollowEvent, GiftEvent, RoomUserSeqEvent

    user = user.lstrip("@")
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
            if follow_ask is not None:
                session = Session(follow_ask)
            status(f"TikTok: connected to @{user}")

        @client.on(CommentEvent)
        async def _(e: CommentEvent):
            handle, name = _who(e.user)
            if handle.lower() != user.lower():  # skip the streamer's own messages
                emit(f"{name}: {e.comment}")
                if session and (ask := session.comment(handle.lower() or name, _following(e.user), time.monotonic())):
                    emit(ask)

        @client.on(FollowEvent)
        async def _(e: FollowEvent):
            if session:
                handle, name = _who(e.user)
                session.follow(handle.lower() or name)

        @client.on(RoomUserSeqEvent)
        async def _(e: RoomUserSeqEvent):
            if session and isinstance(getattr(e, "total", None), int):
                session.viewers(e.total)  # current viewer count; peak_viewers is its max

        @client.on(GiftEvent)
        async def _(e: GiftEvent):
            if e.streaking or not e.gift:  # a combo sends many events; show only the final total
                return
            n = e.repeat_count or 1
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
                retry = 1800 if missing >= 5 else 60
                status(f"TikTok: can't find @{user}. Check the spelling or permission to go LIVE; "
                       f"retrying in {'30 minutes' if retry == 1800 else '1 minute'}.")
            offline = "offline" in type(e).__name__.lower()
            if not missing:
                status(f"TikTok: waiting for @{user} to go live" if offline else f"TikTok: retrying ({type(e).__name__})")
            if not offline:
                log.warning("tiktok: %r", e)
        finally:
            if session:  # stream ended, app stopped or connection dropped; a reconnect starts a new row
                try:
                    session.save(user)
                except OSError as e:
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
    if not re.search(r'"isLive(?:Now)?":true', html_text):
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


async def youtube(handle: str, emit, status) -> None:
    """Chat of the channel's current live stream, read the way the live page itself does (no API key, no login).
    The backlog YouTube sends on (re)connect is skipped so the banner never replays old chat."""
    handle = handle.strip().lstrip("@")
    while True:
        try:
            info = yt_page(await asyncio.to_thread(_yt_get, f"https://www.youtube.com/@{handle}/live"))
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
            status(f"YouTube: retrying ({e.__class__.__name__})")
            log.warning("youtube: %r", e)
            await asyncio.sleep(30)


# ---------------------------------------------------------------- relay: chat source -> one batch per window
async def relay(platform: str, channel: str, on_batch, status, seconds: float = 7, max_lines: int = 3,
                sign_api_key: str = "", channels: dict[str, str] | None = None, follow_ask: str | None = None,
                show_tag: bool = False, on_connect=None) -> None:
    """Run the chat source(s) and call on_batch(text) once per window that had comments. Used by the PC app
    (banner + optional Discord) and by the hosted server (Discord only).

    channels={"tiktok": "@a", "twitch": "b", "youtube": "@c"} reads all of them into the same batches (gifts from any
    sort before comments); status() then gets the per-platform lines joined with " · ". Without it, platform/channel is one source.
    follow_ask goes to tiktok(): the PC app sets it from config, the hosted relay leaves it None.
    show_tag tags only ASK lines; on_connect fires on each source's connected status transition."""
    queue: list[str] = []
    channels = channels or {platform: channel}
    statuses: dict[str, str] = {}

    def source(p: str, c: str):
        def st(s: str) -> None:
            connected = s.partition(":")[2].startswith(" connected to ")
            new_connection = connected and statuses.get(p) != s
            statuses[p] = s
            status(" · ".join(statuses[k] for k in sorted(statuses)))  # "TikTok: … · Twitch: … · YouTube: …"
            if new_connection and on_connect:
                on_connect()
        p, c = p.lower(), c.strip()
        if p == "tiktok":
            return tiktok(c, queue.append, st, sign_api_key, follow_ask)
        return youtube(c, queue.append, st) if p == "youtube" else twitch(c, queue.append, st)

    srcs = [asyncio.ensure_future(source(p, c)) for p, c in channels.items()]
    try:
        while not any(s.done() for s in srcs):  # sources only finish on error or cancel; one failing restarts all
            await asyncio.sleep(seconds)
            if queue:
                lines = queue[:]
                queue.clear()
                on_batch(batch(lines, max_lines, show_tag))
        for s in srcs:
            if s.done():
                s.result()
    finally:
        for s in srcs:
            s.cancel()


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

        def on_batch(text: str) -> None:
            write_banner(text)
            if webhook:
                threading.Thread(target=post_discord, args=(webhook, text), daemon=True).start()

        await relay("", "", on_batch, self._set_status,
                    cfg.getfloat("banner", "seconds", fallback=7), cfg.getint("banner", "max_lines", fallback=3),
                    cfg["chat"].get("tiktok_sign_api_key", ""), channels=channels(cfg),
                    follow_ask=cfg["chat"].get("follow_ask", ""), show_tag=show_tag,
                    on_connect=(lambda: write_banner("LiveChat XR - livechat.deliciouswines.org")) if show_tag else None)

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

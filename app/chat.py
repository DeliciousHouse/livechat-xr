"""Chat sources -> batched banner text -> %LOCALAPPDATA%\\LiveChatXR\\banner.txt.

The OpenXR layer (layer/livechat_xr_layer.cpp) watches banner.txt and shows each new write as a
head-locked banner for [banner] seconds. Comments arriving within one window share one banner.
"""
import asyncio
import configparser
import logging
import os
import random
import re
import ssl
import threading
from pathlib import Path

DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LiveChatXR"
DEFAULTS = {
    "chat": {"platform": "twitch", "channel": "", "tiktok_sign_api_key": ""},
    "games": {"exes": "PopulationONE.exe"},
    "banner": {"seconds": "7", "up": "0.22", "distance": "1.0", "width": "0.62", "max_lines": "3"},
}
log = logging.getLogger("livechatxr")


def load_config() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    cp.read(DIR / "config.ini", encoding="utf-8")
    return cp


def save_config(cp: configparser.ConfigParser) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "config.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        cp.write(f)
    os.replace(tmp, DIR / "config.ini")


def batch(lines: list[str], max_lines: int = 3) -> str:
    """Merge queued comments into one banner; keep it short enough to read mid-game."""
    shown = lines[:max_lines]
    extra = len(lines) - len(shown)
    return "\n".join(shown) + (f"\n+{extra} more" if extra else "")


def write_banner(text: str) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "banner.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, DIR / "banner.txt")  # atomic: the layer never reads a half-written file


# ---------------------------------------------------------------- Twitch (anonymous IRC, no key needed)
_PRIVMSG = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<login>[^!\s]+)!\S+ PRIVMSG #\S+ :(?P<msg>.*)$")


def parse_twitch(line: str) -> tuple[str, str, str] | None:
    """IRC line -> (login, display name, message), or None for anything that isn't chat."""
    m = _PRIVMSG.match(line)
    if not m:
        return None
    name = m["login"]
    for kv in (m["tags"] or "").split(";"):
        if kv.startswith("display-name=") and len(kv) > 13:
            name = kv[13:].replace("\\s", " ")
    return m["login"], name, m["msg"]


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
                elif p := parse_twitch(line):
                    login, name, msg = p
                    if login != channel:  # skip the streamer's own messages
                        emit(f"{name}: {msg}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status(f"Twitch: reconnecting ({e.__class__.__name__})")
            log.warning("twitch: %r", e)
            await asyncio.sleep(10)


# ---------------------------------------------------------------- TikTok (unofficial; TikTokLive library)
async def tiktok(user: str, emit, status, sign_api_key: str = "") -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent

    user = user.lstrip("@")
    if sign_api_key:
        os.environ["SIGN_API_KEY"] = sign_api_key  # read by TikTokLive's signer
    while True:
        client = TikTokLiveClient(unique_id=user)

        @client.on(ConnectEvent)
        async def _(e: ConnectEvent):
            status(f"TikTok: connected to @{user}")

        @client.on(CommentEvent)
        async def _(e: CommentEvent):
            u = e.user
            handle = (getattr(u, "unique_id", None) or getattr(u, "display_id", "") or "") if u else ""
            if handle.lower() == user.lower():
                return
            emit(f"{(u.nickname if u else '') or handle or '?'}: {e.comment}")

        try:
            await client.connect(process_connect_events=False)  # returns when the stream ends
            status(f"TikTok: @{user} went offline, waiting")
        except asyncio.CancelledError:
            if client.connected:
                await client.disconnect()
            raise
        except Exception as e:
            offline = "offline" in type(e).__name__.lower()
            status(f"TikTok: waiting for @{user} to go live" if offline else f"TikTok: retrying ({type(e).__name__})")
            if not offline:
                log.warning("tiktok: %r", e)
        await asyncio.sleep(60)


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
        queue: list[str] = []
        seconds = cfg.getfloat("banner", "seconds", fallback=7)
        max_lines = cfg.getint("banner", "max_lines", fallback=3)
        platform, channel = cfg["chat"]["platform"].lower(), cfg["chat"]["channel"].strip()
        if platform == "tiktok":
            source = tiktok(channel, queue.append, self._set_status, cfg["chat"].get("tiktok_sign_api_key", ""))
        else:
            source = twitch(channel, queue.append, self._set_status)
        src = asyncio.ensure_future(source)
        try:
            while not src.done():
                await asyncio.sleep(seconds)
                if queue:
                    lines = queue[:]
                    queue.clear()
                    write_banner(batch(lines, max_lines))
            src.result()
        finally:
            src.cancel()

    def start(self, cfg: configparser.ConfigParser) -> None:
        self.stop()
        if not cfg["chat"]["channel"].strip():
            self._set_status("Not set up: open Settings and enter your channel")
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

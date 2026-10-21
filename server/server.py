"""LiveChat XR relay server: people enter their TikTok/Twitch channel and a Discord webhook; the server
posts their stream chat to that Discord channel. The Discord app on a standalone Quest pops it up in-game.

Billing: the first FREE_SLOTS sign-ups are founders with all channels free forever. Later sign-ups
run one channel free; a signed Stripe paid checkout enables all saved channels. A cancelled
subscription returns to one free channel without deleting the others.
With DONATE_URL set the relay is free for everyone: every plan runs all saved channels and the pages ask for
donations instead of showing prices (existing paid subscriptions keep working until cancelled).

Sign-up is "Log in or sign up with Discord" when DISCORD_CLIENT_ID and PUBLIC_URL are set: one OAuth consent
(identify email webhook.incoming) identifies the streamer and creates the channel webhook. Without Discord OAuth the
page falls back to a pasted webhook plus a typed name and email. Each registration gets a private manage link, which
is also posted into their Discord channel so it can't get lost (owner decision 2026-10-21, docs/architecture.md).

    DATA_DIR=./data PORT=13300 python server.py      (needs app/chat.py next to it or on PYTHONPATH)
"""
import asyncio
import collections
import hashlib
import hmac
import html
import http.cookies
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote

import chat
import network

DATA = Path(os.environ.get("DATA_DIR", "data"))
PORT = int(os.environ.get("PORT", "13300"))
MAX_REGS = int(os.environ.get("MAX_REGS", "50"))  # hard ceiling, paid or not (one server, one IP)
FREE_SLOTS = int(os.environ.get("FREE_SLOTS", "5"))
PAY_MONTHLY = os.environ.get("PAY_MONTHLY", "")  # Stripe Payment Link URLs
PAY_YEARLY = os.environ.get("PAY_YEARLY", "")
BILLING_URL = os.environ.get("BILLING_URL", "")  # Stripe customer portal login link (manage / cancel)
STRIPE_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DONATE_URL = os.environ.get("DONATE_URL", "")  # set = free for everyone + a donate button (e.g. a Stripe pay-what-you-want link)
DISCORD_ID = os.environ.get("DISCORD_CLIENT_ID", "")  # "Log in or sign up with Discord" (needs PUBLIC_URL too)
DISCORD_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # /admin?key=... lists who is signed up; unset = no admin page
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
GA_ID = os.environ.get("GA_ID", "")  # Google Analytics 4 measurement ID (G-...); unset = no analytics
REGS_PER_DISCORD_USER = 3  # relays one Discord account may own (one per stream/channel); MAX_REGS is still the ceiling
OAUTH_COOKIE = "lcx_oauth"  # binds an OAuth state to the browser that started it (L1)
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{15,22}/[\w-]{40,100}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
CHANNEL = {"tiktok": re.compile(r"^@?[A-Za-z0-9_.]{2,24}$"), "twitch": re.compile(r"^#?[A-Za-z0-9_]{3,25}$"),
           "youtube": re.compile(r"^@?[A-Za-z0-9_.-]{3,30}$")}
LABEL = {"tiktok": "TikTok", "twitch": "Twitch", "youtube": "YouTube"}
log = logging.getLogger("relay")

regs: dict[str, dict] = {}      # token -> {name, email, platform, channel, webhook, discord_channel, created,
                                #           plan: founder|free|paid, subscription}
founders: set[str] = set()     # lifetime slot reservations; deleting a registration does not reopen one
status: dict[str, str] = {}     # token -> latest chat status line
tasks: dict[str, asyncio.Task] = {}
stats: dict[str, dict] = {}     # token -> {posts, fails, last_post, last_error}; saved to stats.json every 30 s
reports: dict[str, float] = {}  # rate limit support reports; no private IDs in the support log
RELAY_VERSION = hashlib.sha256(Path(__file__).read_bytes() + Path(chat.__file__).read_bytes()
                               + Path(network.__file__).read_bytes() + Path(network.backup.__file__).read_bytes()).hexdigest()[:12]
discovery = None  # Constructed by main with the same DATA volume and event loop as chat.
NETWORK_WEB_PRIVATE = False  # Only the isolated test harness enables onboarding; no public activation switch.
network_oauth: dict[str, tuple[float, str]] = {}  # Ephemeral identify state -> registration, never saved.
network_csrf: dict[str, str] = {}  # Per-registration CSRF, never a listing identity or backup field.
STARTED = time.time()
logbuf: collections.deque = collections.deque(maxlen=300)  # recent log lines for /admin


class _Ring(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        logbuf.append((record.created, record.levelname, record.name, record.getMessage()))


def write_json(name: str, obj) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, DATA / name)


def load_log_tail() -> None:
    """Refill the admin log from relay.log (tab-separated: epoch, level, logger, message)."""
    lines: list[str] = []
    for f in (DATA / "relay.log.1", DATA / "relay.log"):
        try:
            lines += f.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            pass
    for line in lines[-logbuf.maxlen:]:
        parts = line.split("\t", 3)
        if len(parts) == 4:
            try:
                logbuf.append((float(parts[0]), parts[1], parts[2], parts[3]))
            except ValueError:
                pass


async def save_stats_loop() -> None:
    last = ""
    while True:
        await asyncio.sleep(30)
        now = json.dumps(stats, sort_keys=True)
        if now != last:
            try:
                write_json("stats.json", stats)
                last = now
            except OSError as e:
                log.warning("saving stats failed: %r", e)

lock = threading.RLock()
oauth: dict[str, tuple[float, dict]] = {}  # state -> (created, sign-up form) while the user is on Discord
loop = asyncio.new_event_loop()


def save() -> None:
    write_json("registrations.json", regs)


def load_registrations() -> None:
    path = DATA / "registrations.json"
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        original = b"{}"
    regs.update(json.loads(original))
    try:
        founders.update(json.loads((DATA / "founders.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        # Back up before either migration write; never replace the original backup on retry.
        backup = DATA / "registrations.pre-founder.json"
        if path.exists() and not backup.exists():
            with backup.open("xb") as f:
                f.write(original)
        legacy = json.loads(backup.read_bytes()) if backup.exists() else regs
        founders.update(t for t, r in legacy.items() if r.get("plan") == "founder")
        eligible = sorted((t for t, r in legacy.items() if r.get("plan", "free") == "free"),
                          key=lambda t: (legacy[t].get("created", 0), t))
        founders.update(eligible[:max(0, FREE_SLOTS - len(founders))])
        write_json("founders.json", sorted(founders))
    changed = False
    for token, r in regs.items():
        p = "founder" if token in founders else plan(r)
        if r.get("plan") != p:
            r["plan"] = p
            changed = True
    if changed:
        save()


def channels(r: dict) -> dict[str, str]:
    """{platform: channel} for a registration; pre-0.2 records only have the single platform/channel pair."""
    return r.get("channels") or {r["platform"]: r["channel"]}


def channel_label(r: dict) -> str:
    return " · ".join(f"{LABEL[p]} {c}" for p, c in channels(r).items())


def plan(r: dict) -> str:
    p = r.get("plan", "free")
    return "free" if p == "pending" else p


def live_channels(r: dict) -> dict[str, str]:
    saved = channels(r)
    return dict(list(saved.items())[:1]) if plan(r) == "free" and not DONATE_URL else saved


def free_used() -> int:
    return max(len(founders), sum(plan(r) == "founder" for r in regs.values()))


def stripe_event(body: bytes, header: str, now: float | None = None) -> dict | None:
    """Verify a Stripe webhook signature (t=...,v1=...) and return the event, or None if it doesn't check out."""
    if not STRIPE_SECRET:
        return None
    pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
    t = next((v for k, v in pairs if k == "t"), "")
    sigs = [v for k, v in pairs if k == "v1"]
    if not t.isdigit() or abs((now or time.time()) - int(t)) > 300:
        return None
    want = hmac.new(STRIPE_SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(want, sig) for sig in sigs):
        return None
    return json.loads(body)


def check_webhook(url: str) -> dict:
    """The webhook object (name, guild_id, channel_id); raises if Discord doesn't know it. Only Discord URLs get here."""
    req = urllib.request.Request(url, headers={"User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def same_channels(a: dict, b: dict) -> bool:
    """Any platform handle in common, ignoring @/# and case (TikTok, Twitch and YouTube handles are case-insensitive)."""
    norm = lambda chans: {(p, c.lstrip("@#").lower()) for p, c in chans.items()}
    return bool(norm(a) & norm(b))


HOSTS = {"tiktok": ("tiktok.com", "www.tiktok.com", "m.tiktok.com"),
         "twitch": ("twitch.tv", "www.twitch.tv", "m.twitch.tv"),
         "youtube": ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com")}


def youtube_owner_handle(url: urllib.parse.ParseResult) -> str | None:
    """Resolve a video or legacy channel link to its owner's current @handle, if available."""
    parts = [x for x in url.path.split("/") if x]
    cid = None
    if url.hostname == "youtu.be" and parts:
        video = parts[0]
    elif parts[:1] == ["watch"]:
        video = parse_qs(url.query).get("v", [""])[0]
    elif parts[:1] in (["live"], ["shorts"]) and len(parts) > 1:
        video = parts[1]
    else:
        video = ""
        if parts[:1] == ["channel"] and len(parts) > 1 and re.fullmatch(r"UC[\w-]{22}", parts[1]):
            cid = parts[1]
        elif parts[:1] in (["c"], ["user"]) and len(parts) > 1:
            m = re.search(r'"externalId":"(UC[\w-]{22})"', chat._yt_get(f"https://www.youtube.com/{parts[0]}/{quote(parts[1])}"))
            cid = m[1] if m else None
    if video:
        if not re.fullmatch(r"[\w-]{6,20}", video):
            return None
        page = chat._yt_get(f"https://www.youtube.com/watch?v={video}")
        m = re.search(r'"ownerProfileUrl":"https?://www\.youtube\.com/(@[\w.-]{3,30})"', page)
        if m:
            return m[1]
        m = re.search(r'"externalChannelId":"(UC[\w-]{22})"', page)
        cid = m[1] if m else None
    if not cid:
        return None
    found = chat.resolve_identity("youtube", "", {"id": cid}) or {}
    return found["handle"] if str(found.get("handle", "")).startswith("@") else None


def normalise_channel(platform: str, channel: str) -> str:
    channel = channel.strip()
    if "/" in channel and channel.partition("/")[0].lower() in HOSTS.get(platform, ()):
        channel = "https://" + channel
    if channel.startswith(("https://", "http://")):
        try:
            url = urllib.parse.urlparse(channel)
        except ValueError:
            return channel  # malformed URLs fail the existing channel syntax check
        if url.hostname in HOSTS.get(platform, ()):
            first = url.path.strip("/").split("/")[0]
            if platform == "youtube" and not first.startswith("@"):
                try:
                    return youtube_owner_handle(url) or channel  # unresolved links fail the syntax check
                except Exception as e:
                    log.warning("youtube link lookup: %s", type(e).__name__)
                    return channel
            channel = first  # also drops a trailing /live
    if platform in ("tiktok", "youtube"):
        return "@" + channel.lstrip("@")
    return channel.lstrip("@#").lower()


def check_channel(platform: str, channel: str) -> bool | None:
    """True = exists (live or offline), False = unavailable, None = inconclusive.

    Use the profile, not TikTokLive's LIVE API: UserNotFoundError there can also mean never streamed.
    Twitch's authenticated Helix API has no cheap anonymous equivalent; IRC remains unchanged.
    YouTube: the channel page answers 404 for an unknown handle.
    """
    if platform == "youtube":
        req = urllib.request.Request(f"https://www.youtube.com/{channel}", headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            return False if e.code == 404 else None
    if platform != "tiktok":
        return True
    req = urllib.request.Request(f"https://www.tiktok.com/{channel}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        text = r.read(2_000_000).decode("utf-8", "replace")
    match = re.search(r'<script\b[^>]*\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', text, re.S)
    if not match:
        return None  # captcha, rate limit, or changed page format: fail open
    detail = json.loads(match[1]).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
    user = detail.get("userInfo", {}).get("user", {}).get("uniqueId", "")
    if user.lower() == channel.lstrip("@").lower():
        return True
    if detail.get("statusCode") in (10202, 10221):  # missing / unavailable (also returned for banned profiles)
        return False
    return None


def remember_identity(token: str, platform: str, found: dict, notify: bool = True, *, expected=None) -> None:
    handle = normalise_channel(platform, found.get("handle", ""))
    if not CHANNEL[platform].fullmatch(handle) or not found.get("id"):
        return
    with lock:
        r = regs.get(token)
        if not r or platform not in channels(r) or (expected is not None and r is not expected):
            return
        old = channels(r)[platform]
        previous = r.setdefault("identities", {}).get(platform, {})
        r["identities"][platform] = {**previous, **found}
        r["channels"] = {**channels(r), platform: handle}
        if r["platform"] == platform:
            r["channel"] = handle
        save()
    if notify and old != handle:
        chat.post_discord(r["webhook"], f"Noticed you renamed to {handle}, still connected.")


def resolve_saved_identities(token: str) -> None:
    r = regs.get(token)
    if not r:
        return
    for p, c in list(channels(r).items()):
        try:
            found = chat.resolve_identity(p, c, r.get("identities", {}).get(p))
            if found and regs.get(token) is r:
                remember_identity(token, p, found, expected=r)
        except Exception as e:
            log.warning("%s identity lookup: %s", p, type(e).__name__)


def status_kind(s: str) -> str:
    s = s.lower()
    if "can't find" in s or "notfound" in s or "not found" in s:
        return "not_found"
    if "429" in s or "ratelimit" in s or "rate limit" in s or "toomanyrequests" in s:
        return "rate_limit"
    if "offline" in s or "waiting" in s:
        return "waiting"
    if "connected to" in s:
        return "connected"
    if "retry" in s or "reconnect" in s or "error" in s:
        return "relay_error"
    return "starting"


def channel_state(token: str, p: str) -> dict:
    return stats.setdefault(token, {}).setdefault("channels", {}).setdefault(p, {})


def diagnostic(token: str) -> dict:
    r = regs[token]
    rows = []
    for p, c in channels(r).items():
        st = channel_state(token, p)
        raw = st.get("status") or next((s for s in status.get(token, "").split(" · ") if s.lower().startswith(p + ":")), "")
        error = stats.get(token, {}).get("discord_error", 0)
        kind = status_kind(raw)
        rows.append({"platform": p, "name": c if CHANNEL[p].fullmatch(c) else "invalid",
                     "status": kind if p in live_channels(r) else "waiting_upgrade",
                     "last_error": "discord_rejected" if error in (401, 404) else "discord_error" if error else
                                   kind if kind in ("not_found", "rate_limit", "relay_error") else "",
                     "last_success": st.get("last_post", 0)})
    return {"plan": plan(r) if plan(r) in ("founder", "free", "paid") else "free", "channels": rows,
            "version": RELAY_VERSION, "uptime_seconds": int(time.time() - STARTED), "reported_at": time.time()}


def support_report(token: str) -> bool:
    with lock:
        if time.time() - reports.get(token, 0) < 60:
            return False
        DATA.mkdir(parents=True, exist_ok=True)
        # No intake or Telegram ops sender is configured. Keep bounded local diagnostics only.
        path = DATA / "support.jsonl"
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, DATA / "support.jsonl.1")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(diagnostic(token)) + "\n")
        reports[token] = time.time()
    return True


def source_status(token: str, p: str, s: str) -> None:
    r = regs.get(token)
    if not r:
        return
    st = channel_state(token, p)
    kind = status_kind(s)
    st["status"] = s
    st["error"] = kind if kind in ("not_found", "rate_limit", "relay_error") else ""
    st["missing"] = st.get("missing", 0) + 1 if kind == "not_found" else 0
    identity = r.get("identities", {}).get(p, {})
    if kind == "not_found" and identity.get("id") and st["missing"] >= 3:
        handle = channels(r)[p]
        if identity.get("alerted_handle") != handle:
            with lock:
                identity["alerted_handle"] = handle
                save()
            chat.post_discord(r["webhook"], f"Handle not found: did you rename? Update {LABEL[p]} here: <{manage_url(token)}>")
    if kind == "relay_error":
        try:
            support_report(token)
        except OSError:
            log.warning("support log unavailable")


def channel_status(token: str, p: str) -> str:
    r = regs[token]
    if p not in live_channels(r):
        return "Waiting for upgrade"
    st = channel_state(token, p)
    discord_error = stats.get(token, {}).get("discord_error", 0)
    if discord_error in (401, 404):
        return "Discord rejected the webhook. Paste a new one below."
    if discord_error:
        return "Discord couldn't accept the last message; queued messages are retrying automatically."
    raw = st.get("status") or next((s for s in status.get(token, "").split(" · ") if s.lower().startswith(p.lower() + ":")), "")
    kind = status_kind(raw)
    if kind == "not_found":
        if r.get("identities", {}).get(p, {}).get("alerted_handle") == channels(r)[p]:
            return "Handle not found: did you rename?"
        return "That channel name wasn't found. Check the spelling below."
    if kind == "rate_limit":
        return "Platform is rate-limiting us; retrying automatically"
    if kind == "relay_error":
        return "Relay problem on our side; we've been notified" if reports.get(token) else "Relay problem on our side; retrying automatically"
    if kind == "waiting":
        return f"Waiting: you're not live on {LABEL[p]} right now"
    if kind == "connected":
        if st.get("last_post"):
            return f"Live: last message sent to Discord {max(0, int((time.time() - st['last_post']) // 60))} min ago"
        return "Connected: waiting for the first chat message."
    return "Starting: checking your channel automatically."


def delivery_status(token: str) -> str:
    st = stats.get(token, {})
    received, posted, dropped = (st.get(key, 0) for key in ("lines_received", "lines_posted", "lines_dropped"))
    pending = max(0, received - posted - dropped)
    if not received:
        return "Waiting for chat messages."
    if not pending and not dropped:
        return f"All {posted} chat messages delivered."
    return f"{posted} of {received} chat messages delivered; {pending} queued; {dropped} older messages collapsed."


async def run(token: str, delay: float = 0) -> None:
    await asyncio.sleep(delay)  # stagger startup: TikTok's sign server rate-limits connects per IP
    r = regs[token]
    tag_pending = False

    def on_connect() -> None:
        nonlocal tag_pending
        tag_pending = True

    def delivery(received=0, posted=0, dropped_backlog=0, ok=None, error=0, platforms=()) -> None:
        if regs.get(token) is not r:
            return
        st = stats.setdefault(token, {"posts": 0, "fails": 0, "last_post": 0, "last_error": ""})
        for key, n in (("lines_received", received), ("lines_posted", posted),
                       ("lines_dropped", dropped_backlog), ("dropped_backlog", dropped_backlog)):
            st[key] = st.get(key, 0) + n
        # One line per window and per post, counts only (never chat text): the relay log is the post timeline.
        if received:
            log.info("%s: %d lines queued", channel_label(r), received)
        if dropped_backlog:
            log.info("%s: %d lines collapsed", channel_label(r), dropped_backlog)
        if ok is None:
            return
        log.info("%s: %s", channel_label(r), f"posted {posted} lines" if ok else f"post failed ({error or '?'})")
        key = "posts" if ok else "fails"
        st[key] = st.get(key, 0) + 1
        if ok:
            st["last_post"] = time.time()
            st["discord_error"] = 0
            st["last_error"] = ""
            for p in platforms:
                channel_state(token, p)["last_post"] = st["last_post"]
        else:
            st["discord_error"] = error or -1
            st["last_error"] = time.strftime("%m-%d %H:%M") + " Discord post failed"

    sender = chat.DiscordQueue(r["webhook"], on_delivery=delivery)
    worker = asyncio.create_task(sender.run())

    def on_lines(lines, platforms=()) -> None:
        nonlocal tag_pending
        tag = ""
        if tag_pending and plan(r) in ("free", "founder"):
            tag = "via LiveChat XR - livechat.deliciouswines.org"
        tag_pending = False
        sender.put(lines, platforms, suffix=tag)

    def on_status(s: str) -> None:
        if status.get(token) != s:
            log.info("%s: %s", channel_label(r), s)
        status[token] = s

    def save_session(row: dict) -> None:
        row = {**row, "registration_id": token}
        try:
            with lock:
                DATA.mkdir(parents=True, exist_ok=True)
                with open(DATA / "sessions.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
        except Exception:
            log.warning("stream stats persistence failed")

    def on_session(row: dict) -> None:
        sender.put([chat.session_summary(row)], count=False)
        threading.Thread(target=save_session, args=(row,), daemon=True).start()

    try:
        while True:
            try:
                await chat.relay(r["platform"], r["channel"], lambda _: None, on_status, channels=live_channels(r),
                                 on_connect=on_connect, on_session=on_session, identities=r.get("identities", {}),
                                 on_identity=lambda p, found: remember_identity(token, p, found, expected=r),
                                 on_source_status=lambda p, s: source_status(token, p, s) if regs.get(token) is r else None,
                                 on_lines=on_lines, platform_icons=r.get("platform_icons", True))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                on_status(f"error, retrying ({type(e).__name__})")
                for p in live_channels(r):
                    source_status(token, p, f"{LABEL[p]}: retrying ({type(e).__name__})")
                log.warning("%s: %s", channel_label(r), type(e).__name__)
                await asyncio.sleep(60)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def start(token: str, delay: float = 0) -> None:
    def go():
        if t := tasks.pop(token, None):
            t.cancel()
        status[token] = "starting…"
        for p in live_channels(regs[token]):
            channel_state(token, p)["status"] = "starting…"
        tasks[token] = loop.create_task(run(token, delay))
    loop.call_soon_threadsafe(go)


def stop(token: str) -> None:
    def go():
        if t := tasks.pop(token, None):
            t.cancel()
    loop.call_soon_threadsafe(go)
    status.pop(token, None)


# ---------------------------------------------------------------- web
# Look: DESIGN.md (approved relay sign-up design). CSS is passed in as a value, so it needs no doubled braces.
CSS = """
:root{--bg:#0f0d16;--surface:#17141f;--chip:#1b1826;--line:#2a2536;--line-field:#332d42;--text:#eceaf4;--text-2:#c9c5d8;
--muted:#a9a5bc;--label:#9a96ad;--faint:#6f6a84;--placeholder:#5f5a72;--primary:#7c4dff;--focus:#8b5cf6;--link:#b39dff;
--accent:#ffc440;--accent-soft:#ffd77a;--error:#5a1f2a;--font:"Bricolage Grotesque",system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;font:400 16px/1.55 var(--font);background:var(--bg);color:var(--text)}
a{color:var(--link)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:6px}
.site{display:flex;justify-content:space-between;align-items:center;max-width:1080px;margin:0 auto;padding:22px 24px;font-size:15px}
.brand{font-weight:800;color:var(--text);text-decoration:none}
.brand img{display:block;width:195px;height:auto}
.site{flex-wrap:wrap;gap:12px}
.site nav a{color:var(--label);text-decoration:none;margin-left:18px}
main{max-width:600px;margin:0 auto;padding:20px 20px 60px}
h1{font-size:32px;line-height:36px;font-weight:800;margin:0 0 14px}
h2{font-size:19px;line-height:24px;font-weight:800;margin:6px 0 12px}
small,.hint{font-size:13px;color:var(--label)}
code{font-size:.9em}
.box{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin:16px 0;text-align:left}
.box.err{background:var(--error);border-color:#7a2a38}
.channel-row{padding:12px 0}.channel-row+.channel-row{border-top:1px solid var(--line)}
.channel-row .hint{color:var(--muted);margin-top:4px}
.account input::placeholder{color:var(--muted)}
.account label,.account .hint,.support .hint{color:var(--muted)}
button.alt.danger{background:var(--error);border:1px solid #7a2a38;min-height:44px}
.actions button{min-height:44px}
.hero{text-align:center;padding-top:10px}
.hero h1{font-size:52px;line-height:52px}
.hero h1 em{font-style:normal;color:var(--accent)}
.sub{color:var(--muted);font-size:18px;line-height:28px;margin:0 0 20px}
.qn{margin:4px auto 0;max-width:360px;display:flex;gap:12px;text-align:left;padding:12px 14px;border-radius:16px;background:#2b2d33;
color:#f2f2f2;font:400 14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;box-shadow:0 8px 24px #0006}
.qn .ic{width:36px;height:36px;border-radius:10px;background:#5865f2;flex:none;display:grid;place-items:center}
.qn .hd{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:#b5b7bd}
.qn .hd b{color:#f2f2f2;font-weight:600}
.qn .ti{font-weight:600;margin:1px 0 2px}
.qn .bd{color:#d9dadd;white-space:pre-line}
.cap{margin:8px 0 18px;font-size:13px;line-height:20px;color:var(--faint)}
.chip{display:inline-block;padding:6px 14px;border-radius:999px;background:var(--chip);border:1px solid #ffc44055;color:var(--accent-soft);font-size:14px;font-weight:600}
.pricing{color:var(--text-2);margin:12px 0 0}
.card{margin-top:28px;background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:30px}
.step{display:grid;grid-template-columns:38px 1fr;gap:16px;padding-bottom:26px;position:relative}
.step:last-child{padding-bottom:0}
.step:not(:last-child)::before{content:"";position:absolute;left:18px;top:42px;bottom:4px;width:2px;background:var(--line)}
.n{width:38px;height:38px;border-radius:12px;background:var(--accent);color:#1a1300;display:grid;place-items:center;font-weight:800;font-size:17px}
.step.later .n{background:var(--line);color:var(--accent)}
.step p{margin:0;color:var(--text-2)}
label{display:block;font-size:13px;font-weight:600;color:var(--label);margin:12px 0 6px}
label.check{display:flex;gap:8px;align-items:center;font-weight:500;color:var(--text)}
input[type=text],input[type=email],input[type=url],input:not([type]){width:100%;padding:12px 14px;font:inherit;color:var(--text);
border:1px solid var(--line-field);border-radius:12px;background:var(--bg)}
input::placeholder{color:var(--placeholder)}
input:focus{outline:3px solid #8b5cf655;border-color:var(--focus)}
input[readonly]{color:var(--muted)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
button{padding:12px 18px;border:0;border-radius:14px;background:var(--primary);color:#fff;font:800 16px/1.2 var(--font);cursor:pointer;
transition:filter .12s ease,transform .12s ease}
button:hover{filter:brightness(1.08)}button:active{transform:translateY(1px)}
button.alt{background:var(--line)}
.cta{width:100%;padding:16px;font-size:18px;box-shadow:0 10px 30px -10px #7c4dffaa}
.cta.alt{margin-top:10px;font-size:16px;box-shadow:none}
details{margin-top:10px}
summary{cursor:pointer;font-size:14px;color:var(--label);text-decoration:underline;text-underline-offset:3px}
details ol{margin:8px 0 0;padding-left:20px;color:var(--text-2);font-size:14px}
form.inline{display:inline}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
footer{margin-top:26px;font-size:13px;line-height:20px;color:var(--faint);text-align:center}
footer a{color:var(--label)}
@media (min-width:769px){.hero h1{margin-left:-40px;margin-right:-40px}}
@media (max-width:768px){.hero h1{font-size:44px;line-height:46px}}
@media (max-width:600px){.hero h1{font-size:36px;line-height:39px}.sub{font-size:16px;line-height:25px}.two{grid-template-columns:1fr}.card{padding:22px}}
@media (max-width:375px){main{padding-left:14px;padding-right:14px}.card{padding:18px}.step{grid-template-columns:32px 1fr;gap:12px}
.n{width:32px;height:32px;font-size:15px}.step:not(:last-child)::before{left:15px;top:36px}.site nav a{margin-left:12px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark">
<title>LiveChat XR for Discord</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&display=swap" rel="stylesheet">
<style>{css}</style>{head}</head><body>
<header class="site"><a class="brand" href="/"><img src="/brand/header-dark.svg" width="195" height="48" alt="LiveChat XR"></a>
<nav aria-label="Site"><a href="https://github.com/DeliciousHouse/livechat-xr">GitHub</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav></header>
<main>{body}</main></body></html>"""

DISCORD_ICON = ('<svg viewBox="0 0 24 24" width="22" height="22" fill="#fff" aria-hidden="true"><path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/></svg>')

# The Quest shows a stock Meta notification from the Discord app (no custom styling); the body is one chat.batch().
HOME = """<div class="hero">
<h1 data-pretext>Stream chat on your Quest, <em>through Discord.</em></h1>
<p class="sub" data-pretext>TikTok LIVE, Twitch, YouTube or any mix. No PC needed. Requires Discord on your Quest.</p>
<figure style="margin:0"><div class="qn" role="img" aria-label="Example Quest notification from Discord in #stream-chat: Sam sent Rose x10, BigFan: nice shot!, Mike: GG, plus 2 more">
<div class="ic" aria-hidden="true">{icon}</div><div aria-hidden="true"><div class="hd"><b>Discord</b><span>now</span></div>
<div class="ti">#stream-chat · LiveChat XR</div><div class="bd">🎁 Sam sent Rose x10
BigFan: nice shot!
Mike: GG
+2 more</div></div></div>
<figcaption class="cap" data-pretext>What you see in the headset: a normal Quest notification, mid-game.</figcaption></figure>
{pricing}</div>
{msg}
<form class="card" method="post" action="/register" aria-label="Set up LiveChat XR for Discord">
<section class="step" aria-labelledby="s1"><div class="n" aria-hidden="true">1</div><div><h2 id="s1">Your channel</h2>
<label for="tiktok">TikTok handle</label><input type="text" id="tiktok" name="tiktok" value="{tiktok}" placeholder="@yourhandle or profile URL"
maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="twitch">Twitch channel</label><input type="text" id="twitch" name="twitch" value="{twitch}" placeholder="yourchannel or twitch.tv URL"
maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="youtube">YouTube handle</label><input type="text" id="youtube" name="youtube" value="{youtube}" placeholder="@yourhandle or youtube.com URL"
maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="hint">{channels_hint}</div>
{identity}
</div></section>
<section class="step" aria-labelledby="s2"><div class="n" aria-hidden="true">2</div><div><h2 id="s2">Connect Discord</h2>
{discord}
</div></section>
<section class="step later" aria-labelledby="s3"><div class="n" aria-hidden="true">3</div><div><h2 id="s3">On your Quest</h2>
<p data-pretext>Install Discord on your Quest if you have not already, open the channel, and set notifications to <b>All Messages</b>.</p></div></section>
</form>
<p style="text-align:center"><a href="/recover">Already set up? Log in</a></p>
<footer>Your Discord username and email (or the name and email you type) are only used to know who is using it
and to let you log in.{ga_note} Your webhook URL is only used to post your chat.
Mentions are disabled, so chat can't ping anyone.<br><a href="https://github.com/DeliciousHouse/livechat-xr">Open source on GitHub</a> ·
<a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></footer>"""

# Resize-aware heights for the hero text (Pretext, MIT, served from /pretext.js). If it fails to load, plain CSS layout stands.
PRETEXT_JS = """<script type="module">
import { prepare, layout } from "/pretext.js";
await document.fonts.ready;
const els = [...document.querySelectorAll("[data-pretext]")], prepared = new Map();
const prep = el => prepared.set(el, prepare(el.textContent, getComputedStyle(el).font));
function relayout() {
  for (const [el, handle] of prepared) {
    el.style.height = "";
    const cs = getComputedStyle(el);
    el.style.height = layout(handle, el.clientWidth, parseFloat(cs.lineHeight)).height + "px";
  }
}
els.forEach(prep);
for (const q of ["(max-width:375px)", "(max-width:600px)", "(max-width:768px)"])
  matchMedia(q).addEventListener("change", () => { els.forEach(prep); relayout(); });
new ResizeObserver(relayout).observe(document.querySelector("main"));
relayout();
</script>"""
try:
    PRETEXT = (Path(__file__).with_name("pretext.js")).read_bytes()
except OSError:
    PRETEXT = b""

PRIVACY = """<h1>Privacy</h1>
<p>LiveChat XR relays TikTok LIVE, Twitch and YouTube chat to your chosen Discord channel.
The private Pop1 Live discovery test is separate: a relay account does not opt you into a listing.</p>
<h2>Chat relay</h2><p>We store your Discord identity when you sign up or log in with Discord (your Discord user ID, username, display name
and email if Discord shares a verified one), or the name and email you type where Discord sign-in is not offered; supplied channels,
resolved platform identities, Discord channel/server IDs and webhook, private manage credential, plan and any Stripe subscription ID.
These run your relay and let you manage it. Stripe handles payment details. Chat text passes to Discord without being stored here;
delivery counts, status and troubleshooting logs are retained. Discord receives the messages you ask us to relay.</p>
<h2>Stream summaries and census</h2><p>Relay session summaries store the channel, registration association, start/end times,
comment and distinct-chatter totals, gift/support-event totals and available peak-viewer/follow totals.
Chatter identifiers used for these counts stay in connection memory, not the summary file.
Discovery's separate census observes public Twitch and YouTube channel identities, titles, game, live status and raw per-platform
viewer counts to find Population: ONE streams. Observation alone never authorizes a public listing.</p>
<h2>Private discovery listings</h2><p>Explicit opt-in and channel proof are separate from Discord sign-in and founder review.
Listing records contain the Discord user ID, proved streamer-supplied handles, in-game name, IANA timezone,
consent/review/removal state, DM availability, alert preference and optional one-per-streamer “I'd pay” flag.
The relay keeps only the listing association and setup consent alongside its own registration. OAuth credentials and proof challenges
are temporary, not saved in the listing database or snapshots. Eligible listings and Watch on Twitch / Watch on YouTube links
can appear in the configured private Discord test; alerts require opt-in. TikTok discovery is not enabled.</p>
<h2>YouTube and Google</h2><p>Private discovery and ownership proof use official YouTube API Services.
The chat relay separately reads YouTube's live-chat page endpoint. LiveChat XR is not affiliated with YouTube, Twitch or TikTok.
See the <a href="https://policies.google.com/privacy">Google Privacy Policy</a> and
<a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>.</p>
<h2>Removal and retention</h2><p>For discovery, run <code>/optout</code> as the linked Discord account or untick
<b>List me</b> and save on your private manage page. The shared removal operation immediately excludes the listing,
deletes its active platform data and attempts to edit the list and naming pings. Failed remote edits are reported and retried a bounded
number of times; already-delivered notifications cannot be recalled. The active listing record is deleted within 7 days unless you opt back in.
Discovery platform data expires after at most 24 hours; ended streams are cleared on a successful poll. Live visibility expires sooner,
after 30 minutes. Memory-only peaks expire within 24 hours. New sanitized listing snapshots exclude platform data and restore no verified binding.</p>
<p>For the relay, <b>Stop and delete</b> on the private manage page removes the active registration immediately; it does not cancel
Stripe billing. Session summaries, logs and historic backups are separate from the active registration. Their complete erasure timing
has not been verified. The prior relay policy stated backup copies roll off within 90 days; the operations record describes 30-day
server archives, offsite copies and controller rollback archives. Those different scopes are not proof of a single erasure deadline.
In particular, historic backups are not proved erased within 7 days. We do not claim that active listing deletion erases those archives.</p>
<h2>Analytics and fonts</h2><p>{ga} Private listing/proof pages and Watch redirects do not collect discovery visitor analytics:
no visitor cookies, IP/UA tracking, fingerprinting or click-total proxy. E6 visitor measurement is unavailable.
Google Fonts supplies this page's font, so your browser contacts Google's servers.
Discord sign-in sets one short-lived cookie (15 minutes) that ties the sign-in to the browser that started it; it is not used for tracking.</p>
<h2>Questions or deletion help</h2><p>Use the existing <a href="https://github.com/DeliciousHouse/livechat-xr/issues">GitHub issue route</a>
for help. Issues are public: never include a private manage link, webhook, OAuth code, email or other private data.</p>
<p><a href="/terms">Terms and service information</a> · <a href="/">Back</a></p>"""

TERMS = """<h1>Terms and service information</h1>
<p>LiveChat XR delivers stream chat from TikTok LIVE, Twitch and YouTube to a Discord channel you configure.
The Windows app provides a separate PC VR overlay. Discord delivery does not guarantee a headset notification.</p>
<h2>Platform services</h2><p>YouTube API Services are used by the private discovery and channel-proof implementation.
Use of YouTube is subject to the <a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>;
see also the <a href="https://policies.google.com/privacy">Google Privacy Policy</a>.
Watch links name their destination platform. LiveChat XR is not affiliated with these platforms.</p>
<h2>Private Pop1 Live test</h2><p>Discovery is separate from the relay. A listing needs explicit consent, proof of channel control
and separate review. It is free in the private test; a demand tap is not a purchase or a subscription.
Public activation, TikTok discovery, quota approval and platform-sharing clearance are not claimed by these pages.</p>
<h2>Your controls</h2><p>Use your private manage page for relay settings and Stop and delete. Use <code>/optout</code>
or the listing's List me control to remove discovery participation. Already-delivered notifications cannot be recalled.
Deleting a relay does not cancel an existing paid subscription; use its billing control first.</p>
<p>This page describes the implemented service and API use; it does not add new waivers or grant new permissions.
Data handling and retention limitations are in <a href="/privacy">Privacy</a>.
For help, use the <a href="https://github.com/DeliciousHouse/livechat-xr/issues">existing GitHub issue route</a> without private data.</p>"""

MANAGE = """<h1>Your relay account</h1>{msg}{checklist}
<section class="box" aria-labelledby="relay-status"><h2 id="relay-status">Channel status</h2>{channels}
<div class="channel-row"><b>Discord:</b> webhook “{dname}”
<div class="hint">Use Send test message to check delivery.</div>
<div class="hint" role="status">{delivery}</div></div></section>{billing}
<p>Keep this page's link; it's also posted in your Discord channel.{login} Leave it running: it picks up your chat
whenever you go live.</p>
{settings}
<section class="support"><h2>Need help?</h2><p class="hint">Send a diagnostic with channel names, status and relay version only.
No email, webhook, private link or chat messages are included.</p>
<div class="actions"><form class="inline" method="post" action="/m/{token}/test"><button class="alt">Send test message</button></form>
<form class="inline" method="post" action="/m/{token}/report"><button class="alt">Something's wrong</button></form></div></section>
<div class="actions">
<form class="inline" method="post" action="/m/{token}/delete" onsubmit="return confirm('Stop posting chat to Discord?')">
<button class="alt danger">Stop and delete</button></form></div>
<p class="hint">Deleting your relay does not cancel a paid subscription. Use Manage billing first.</p>
<p><a href="/">Set up another channel</a></p>"""

# Shown open right after sign-up, collapsed afterwards. Mirrors the user guide's Quest troubleshooting row.
QUEST_CHECKLIST = """<details class="box"{open}><summary><b>{title}</b></summary><ol>
<li>Install Discord from the Meta Horizon Store on your Quest and sign in with this same Discord account.</li>
<li>In Discord, open {channel}. You should see the "LiveChat XR connected" message there; if you don't, you are on a
different Discord account or channel. Set the channel's notifications to <b>All Messages</b>, and check that neither the
channel nor the server is muted.</li>
<li>In your Quest settings, allow notifications from Discord and turn off Do Not Disturb.</li>
<li>Put your headset on and press <b>Send test message</b>. A Discord pop-up should appear within a few seconds.</li>
<li>Open your game and send another test, so you know it shows up in-game before you go live.</li></ol>
<form class="inline" method="post" action="/m/{token}/test"><button>Send test message</button></form></details>"""

# Existing Dora controllers parse this hidden legacy status contract, not user-facing copy.
PROBE_STATUS = '<div hidden aria-hidden="true"><b>Status:</b> {status}</div>'


WEBHOOK_FIELD = ('<label for="webhook">Discord webhook URL</label><input type="url" id="webhook" name="webhook" '
                 'placeholder="https://discord.com/api/webhooks/…" value="{webhook}" maxlength="200"{req}>')
DISCORD_BUTTON = "Log in or sign up with Discord"
# Without Discord OAuth (self-hosted or local dev) the page asks for a typed name/email and a pasted webhook instead.
TYPED_IDENTITY = """<div class="two"><div><label for="name">Name</label><input type="text" id="name" name="name" value="{name}" required maxlength="60" autocomplete="name"></div>
<div><label for="email">Email</label><input type="email" id="email" name="email" value="{email}" required maxlength="200" autocomplete="email"></div></div>"""
MANUAL_WEBHOOK = """<details open><summary>Getting a webhook URL (1 minute)</summary><ol>
<li>In Discord, open a server you own (a new private one is fine) and make a channel like <code>#stream-chat</code>.</li>
<li>Channel settings (gear) → <b>Integrations</b> → <b>Webhooks</b> → <b>New Webhook</b> → <b>Copy Webhook URL</b>.</li>
<li>Paste it above and press Connect. You'll get a test message in that channel.</li></ol></details>"""


def discord_on() -> bool:
    return bool(DISCORD_ID and PUBLIC_URL)  # Discord rejects a relative redirect_uri ("Not a well formed URL")


def state_cookie(path: str, state: str) -> str:
    """Single-use OAuth state, bound to this browser for the 900 s the state itself lives (L1)."""
    return f"{OAUTH_COOKIE}={state}; Max-Age=900; Path={path}; SameSite=Lax; HttpOnly; Secure"


def home(msg: str = "", form: dict | None = None) -> bytes:
    form = form or {}
    chans = {p: form.get(p, "") for p in LABEL}
    if form.get("platform") in LABEL and form.get("channel"):  # older single-platform form
        chans[form["platform"]] = form["channel"]
    values = {k: html.escape(form.get(k, "")) for k in ("name", "email", "webhook")} | {p: html.escape(c) for p, c in chans.items()}
    if discord_on():
        identity = ""  # name and email come from Discord
        discord = ('<p class="hint">No Discord server yet? Discord only lists servers you own or manage. '
                   '<a href="https://discord.com/channels/@me" target="_blank" rel="noopener">Open Discord</a>, tap <b>+</b> → '
                   '<b>Create My Own</b>, add a channel like <code>#stream-chat</code>, then come back here.</p>'
                   f'<button class="cta">{DISCORD_BUTTON}</button><p class="hint">Discord asks which server '
                   'and channel to post in. Pick a channel in a server you own, e.g. a new <code>#stream-chat</code>. '
                   'Discord also shares your username and email, so you can log in here later.</p>'
                   '<p class="hint">Already set up? Leave the channels empty and press the button to log in.</p>')
    else:
        identity = TYPED_IDENTITY.format(name=values["name"], email=values["email"])
        discord = (WEBHOOK_FIELD.format(req=" required", webhook=values["webhook"])
                   + '<button class="cta" style="margin-top:14px">Connect</button>' + MANUAL_WEBHOOK)
    ga_note = " Visits are counted with Google Analytics." if GA_ID else ""
    if DONATE_URL:
        pricing = ('<span class="chip">Free for every streamer, every channel.</span>'
                   '<p class="pricing">LiveChat XR runs on donations. If it helps your streams, '
                   f'<a href="{html.escape(DONATE_URL)}">chip in what you like</a>.</p>'
                   '<p class="hint">Posts carry a one-line LiveChat XR tag.</p>')
        channels_hint = "Fill in any of them. All of them run in the same Discord channel."
    else:
        pricing = (f'<span class="chip">First {FREE_SLOTS} streamer{"" if FREE_SLOTS == 1 else "s"}: everything free.</span>'
                   '<p class="pricing">After that: 1 channel free, multiple channels on a paid plan ($3/month or $25/year).</p>'
                   '<p class="hint">Free posts carry a one-line tag; paid posts do not.</p>')
        channels_hint = ("Fill in any of them. On the free plan, only the first filled channel runs (TikTok, then Twitch, then YouTube).\n"
                         "Founders and paid plans run all saved channels in the same Discord channel.")
    return page(HOME.format(msg=msg, discord=discord, identity=identity, ga_note=ga_note, icon=DISCORD_ICON,
                            pricing=pricing, channels_hint=channels_hint, **values) + PRETEXT_JS, ga="/")


def discord_oauth(code: str, callback: str = "/discord/callback") -> tuple[dict, dict]:
    """OAuth code -> (token response, /users/@me). The token response carries the new webhook when webhook.incoming was
    granted. Raises unless Discord returned a bearer token and a numeric user ID."""
    body = urllib.parse.urlencode({"client_id": DISCORD_ID, "client_secret": DISCORD_SECRET,
        "grant_type": "authorization_code", "code": code, "redirect_uri": f"{PUBLIC_URL}{callback}"}).encode()
    req = urllib.request.Request("https://discord.com/api/oauth2/token", body,
                                 {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=10) as response:
        credential = json.load(response)
    if credential.get("token_type", "").lower() != "bearer" or not credential.get("access_token"):
        raise ValueError("identify failed")
    req = urllib.request.Request("https://discord.com/api/users/@me", headers={
        "Authorization": "Bearer " + credential["access_token"], "User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=10) as response:
        identity = json.load(response)
    if not isinstance(identity.get("id"), str) or not re.fullmatch(r"\d{1,22}", identity["id"]):
        raise ValueError("identify failed")
    return credential, identity


def discord_identify(code: str) -> str:
    """Separate identify flow; neither a webhook nor a posted ID proves an owner."""
    return discord_oauth(code, "/network/discord/callback")[1]["id"]


def discord_person(me: dict) -> dict:
    """The fields a registration keeps from /users/@me. Email only when Discord says it is verified (it can be null)."""
    email = me.get("email") if me.get("verified") is True and isinstance(me.get("email"), str) else ""
    username = str(me.get("username") or "")[:60]
    return {"id": me["id"], "username": username, "name": str(me.get("global_name") or username or "Discord user")[:60],
            "email": email.lower() if EMAIL.match(email) else ""}


def listing_profile(r):
    """Only server-owned association metadata selects a private profile."""
    if not discovery or not r.get("network_listing"): return None
    profile = discovery.store.profile(r["network_listing"])
    if profile and profile["discord_id"] != r.get("network_owner"):
        raise ValueError("listing ownership conflict")
    return profile


def save_listing_association(r, **fields):
    before = dict(r)
    r.update(fields)
    try: save()
    except OSError:
        r.clear(); r.update(before)
        raise


def listing_section(token, r, errors=None, form=None):
    if not NETWORK_WEB_PRIVATE and not r.get("network_listing"): return ""
    errors, form = errors or {}, form or {}
    e = html.escape
    with lock:
        for old in set(network_csrf) - regs.keys(): network_csrf.pop(old, None)
        csrf = network_csrf.setdefault(token, secrets.token_urlsafe(32))
    try:
        profile = listing_profile(r)
        verified = bool(profile and any(c["listing_id"] == profile["id"] and c["verified"]
                                       for c in discovery.store.channels()))
    except Exception:
        return '<section id="network-listing" class="card"><h2>Pop1 Live listing</h2><p>Status: Needs attention</p><p>Listing storage is unavailable. Try again shortly.</p></section>'
    requested = bool(r.get("network_requested")) and not (profile and profile["review_status"] == "removed")
    next_step = 1 if not requested else 2 if not profile else 3 if not profile["ign"] else 4 if not verified else 5
    removed = bool(profile and profile["review_status"] == "removed")
    state = ("Removed" if removed else "Not requested" if not requested else
             "Needs attention" if profile and profile["review_status"] not in ("pending", "reviewed") else
             f"Setting up (step {next_step} of 5)" if next_step < 5 else
             "Listed when you're live" if profile["review_status"] == "reviewed" else "Waiting for review (within 24h)")
    def post(action, content):
        return (f'<form method="post" action="/m/{token}/network"><input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="action" value="{action}">{content}</form>')
    def button(label, step):
        return f'<button class="{"network-next" if next_step == step else "alt"}">{label}</button>'
    active = requested and not removed
    consent = post("consent", '<label class="check"><input type="checkbox" name="listed" value="yes"'
                   + (' checked' if active else '') + (' disabled' if not NETWORK_WEB_PRIVATE and not active else '')
                   + '>List me in Pop1 Live</label>' + button("Save listing consent", 1))
    if not NETWORK_WEB_PRIVATE and not active: consent = '<p>New listing requests are paused.</p>'
    if r.get("network_listing"):
        consent += post("optout", '<button class="alt">Remove my listing now</button>')
    link = '<p>Discord identity linked. This is not channel proof.</p>' if profile else (
        post("link", button("Link Discord", 2)) if next_step == 2 else '<p>Link your own Discord account next.</p>')
    ign = form.get("ign", profile["ign"] if profile else "")
    zone = form.get("timezone", profile["timezone"] if profile else "America/Los_Angeles")
    other = form.get("other_timezone", zone if zone not in dict(network.ZONES).values() else "")
    options = ''.join(f'<option value="{z}"' + (' selected' if zone == z else '') + '>'
                      + e(f'{city} · {network.datetime.now(network.ZoneInfo(z)):%H:%M}') + '</option>' for city, z in network.ZONES)
    options += '<option value="Other"' + (' selected' if zone not in dict(network.ZONES).values() else '') + '>Other</option>'
    fields = ('<label for="ign">In-game name (required)</label>'
              f'<input id="ign" name="ign" type="text" required maxlength="64" value="{e(ign)}" aria-describedby="ign-error">'
              f'<p id="ign-error" class="hint" role="status">{e(errors.get("ign", ""))}</p>'
              '<label for="timezone">Timezone</label><select id="timezone" name="timezone" aria-describedby="timezone-error">'
              + options + '</select><div id="other-zone"><label for="other_timezone">Other IANA timezone</label>'
              f'<input id="other_timezone" name="other_timezone" type="text" value="{e(other)}" aria-describedby="timezone-error">'
              '<p class="hint">For Other, enter an IANA name such as Pacific/Auckland.</p></div>'
              f'<p id="timezone-error" class="hint" role="status">{e(errors.get("timezone", ""))}</p>' + button("Save profile", 3))
    profile_form = post("profile", fields) if profile and requested else '<p>Your game name and local timezone.</p>'
    proof = ('<p>Channel ownership verified ✓</p>' if verified else
             '<p>In the private Discord test server, use the account you linked here and run '
             '<code>/optin platform:Twitch handle:yourchannel</code> or choose YouTube and your @handle. '
             'The bot gives you a 15-minute code and checks every minute. Return here after verification. '
             'TikTok discovery is not available.</p>' if next_step == 4 else '<p>Prove control of your Twitch or YouTube channel.</p>')
    if next_step == 4: proof += post("prove", button("Show proof instructions", 4))
    review = ('<p>' + e(network.APPROVED if state == "Listed when you're live" else network.PENDING) + '</p>'
              if next_step == 5 else '<p>A separate founder review follows channel proof.</p>')
    sections = []
    for step, (title, content) in enumerate(zip(
            ("List me", "Link Discord", "In-game name + timezone", "Prove your channel", "Review"),
            (consent, link, profile_form, proof, review)), 1):
        done = step < next_step or (step == 5 and state == "Listed when you're live")
        current = ' aria-current="step"' if step == next_step and next_step < 5 else ''
        sections.append(f'<div class="step{" later" if step > next_step else ""}"{current}><div class="n">{step}</div>'
                        f'<div><h2>{"✓ " if done else ""}{title}</h2>{content}</div></div>')
    return ('<section id="network-listing" class="card"><h2>Pop1 Live listing</h2>'
            f'<p role="status"><strong>Status: {state}</strong></p><p class="hint">Private test · separate from chat relay · free, no checkout.</p>'
            + ''.join(sections) + '</section><style>'
            '#network-listing .step>div:last-child{min-width:0}#network-listing p{overflow-wrap:anywhere}'
            '#network-listing .later p{color:var(--label)}#network-listing .later .n{background:var(--accent);color:#1a1300}'
            '#network-listing input[type=text],#network-listing select{border:2px solid var(--faint);min-height:44px;'
            'width:100%;padding:10px;border-radius:12px;background:var(--bg);color:var(--text);font:inherit}'
            '#network-listing button,#network-listing .check{min-height:44px;margin-top:10px}'
            '#network-listing :focus-visible{outline:3px solid var(--focus)}'
            '#network-listing .hint{color:var(--label)}.site{flex-wrap:wrap;gap:8px}</style>'
            + ('<script>const z=document.getElementById("timezone"),o=document.getElementById("other-zone");'
               + ('const local=Intl.DateTimeFormat().resolvedOptions().timeZone;z.value=[...z.options].some(x=>x.value===local)?local:"Other";if(z.value==="Other")document.getElementById("other_timezone").value=local;'
                  if profile and not profile["ign"] and not form else '')
               + 'function showZone(){o.hidden=z.value!=="Other";}z.addEventListener("change",showZone);showZone();</script>'
               if profile and requested else ''))


def page(body: str, ga: str = "", event: str = "") -> bytes:
    """ga: the path Google Analytics reports for this page (never the real URL, which can carry a manage token)."""
    head = ""
    if GA_ID and ga:
        gid = html.escape(GA_ID)
        head = (f'<script async src="https://www.googletagmanager.com/gtag/js?id={gid}"></script><script>'
                "window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}gtag('js',new Date());"
                f"gtag('config','{gid}',{{page_location:location.origin+'{ga}',page_referrer:document.referrer.split('?')[0]}});"
                f"{event}</script>")
    return PAGE.format(css=CSS, body=body, head=head).encode()


def note(text: str, err: bool = False) -> str:
    return f'<div class="box{" err" if err else ""}" role="{"alert" if err else "status"}">{html.escape(text)}</div>'


def manage_url(token: str) -> str:
    return f"{PUBLIC_URL}/m/{token}" if PUBLIC_URL else f"/m/{token}"


def settings_form(token: str, form: dict) -> str:
    e = html.escape
    inputs = "".join(f'<label for="{p}">{LABEL[p]} channel</label><input type="text" id="{p}" name="{p}" '
                     f'value="{e(form.get(p, ""))}" maxlength="200" autocapitalize="off" spellcheck="false">' for p in LABEL)
    return (f'<form class="card account" method="post" action="/m/{token}/save"><h2>Edit settings</h2>'
            '<div class="two"><div><label for="name">Name</label><input type="text" id="name" name="name" '
            f'value="{e(form.get("name", ""))}" required maxlength="60" autocomplete="name"></div>'
            '<div><label for="email">Email</label><input type="email" id="email" name="email" '
            f'value="{e(form.get("email", ""))}"' + ('' if regs.get(token, {}).get("discord_id") else ' required')
            + ' maxlength="200" autocomplete="email"></div></div>'
            + inputs + WEBHOOK_FIELD.format(webhook="", req="").replace("https://discord.com/api/webhooks/…", "Paste a new webhook URL")
            + '<input type="hidden" name="icons_form" value="1"><label class="check"><input type="checkbox" name="platform_icons" value="1"'
            + (' checked' if form.get("platform_icons", True) not in (False, "") else '') + '> Start each message with a platform emoji '
            '(🎵 TikTok, 🟣 Twitch, ▶️ YouTube)</label>'
            + '<p class="hint">Leave the webhook blank to keep the current one. Paste a new one to change Discord channels. '
            'Changing your Discord username needs no action.</p><button class="cta">Save settings</button></form>')


def recovery_page(msg: str = "") -> bytes:
    body = '<h1>Log in</h1>' + msg
    if discord_on():
        body += ('<p>Log in with the Discord account you signed up with to open your private manage page.</p>'
                 '<form method="post" action="/recover"><button class="cta">Log in with Discord</button></form>'
                 '<p class="hint">Set up before Discord sign-in? Your private manage link is in the Discord channel you connected. '
                 'Or sign up again with Discord for the same stream in the same Discord server: that links your existing relay '
                 'to your Discord account instead of making a new one.</p>')
    else:
        body += '<p>Discord log-in is not available here. Look for your private manage link in the Discord channel you connected.</p>'
    return page(body + '<p><a href="/">Back to setup</a></p>')


def relay_list(found: list[tuple[str, str]]) -> bytes:
    return page('<h1>Your relays</h1>' + "".join(f'<p><a href="/m/{t}">{html.escape(label)}</a></p>' for t, label in found))


def ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


def backup_line() -> str:
    """The nightly backup jobs stamp /data/last_backup (server copy) and /data/last_offsite (copy pulled to the PC)."""
    parts = []
    for name, label in (("last_backup", "Backup on server"), ("last_offsite", "copy on PC")):
        try:
            ts = float((DATA / name).read_text().strip())
        except (OSError, ValueError):
            ts = 0
        stale = time.time() - ts > 36 * 3600
        parts.append(f'<span{" style=color:#ff8a8a" if stale else ""}>{label}: {ago(ts)}{" ⚠" if stale else ""}</span>')
    return ", ".join(parts) + "."


def network_incidents() -> str:
    e = html.escape
    state = discovery.snapshot() if discovery else {"enabled": False, "missing": [], "error": "not started", "sources": {}}
    problems = []
    if not state["enabled"]:
        reason = "missing " + ", ".join(state["missing"]) if state["missing"] else state["error"] or "stopped"
        problems.append("Discovery network off — " + reason + ". Add it to ~/.livechat-xr.env on Dora, then restart the relay.")
    for platform, source in state["sources"].items():
        if source["delayed"]:
            since = source["incident_since"]
            stamp = network.datetime.fromtimestamp(since, network.PACIFIC).strftime("%I:%M%p").lstrip("0").lower() + " PT"
            minutes = max(0, int((time.time() - since) / 60))
            problems.append(f'{network.SOURCE_NAMES[platform]} data stale since {stamp} ({minutes} min ago) — '
                            + (source["error"] or "waiting for two successful polls") + ".")
    if discovery and discovery.store.path.exists():
        try:
            closed = discovery.store.dm_closed_count()  # HTTP thread, never the relay loop.
            if closed: problems.append(f'DMs closed: {closed} listing' + ("s" if closed != 1 else ""))
        except Exception:
            problems.append("Discovery storage unavailable — check the relay log.")
    running = loop.is_running()
    if not running: problems.append("Chat relay loop is not running — check the relay process.")
    if problems:
        return ('<section id="network-incidents" class="box network-incidents" aria-label="Service incidents">'
                f'<p><strong>Chat relay: {"running" if running else "not running"}</strong></p><ul>'
                + "".join(f'<li><span aria-hidden="true">⚠</span> {e(p)}</li>' for p in problems) + '</ul></section>')
    current = bool(state["sources"]) and all(s["last_success"] > time.time() - 900 and not s["error"]
                                            for s in state["sources"].values())
    return '<p class="network-summary">Discovery: ' + ("all sources current" if current else "waiting for first successful polls or retry") + '</p>'


def admin_body() -> str:
    e = html.escape
    live = sum("connected" in status.get(t, "") for t in regs)
    paid = sum(plan(r) == "paid" for r in regs.values())
    rows = []
    for t, r in sorted(regs.items(), key=lambda kv: -kv[1]["created"]):
        st = stats.get(t, {})
        g = {"google": " (Google)", "discord": " (Discord)"}.get(r.get("signin"), "")
        rows.append(f"<tr><td>{e(r.get('name', ''))}<br><small>{e(r.get('email', ''))}{g}</small></td>"
                    f"<td>{e(channel_label(r))}<br><small>Discord: {e(r['discord_channel'])}</small></td>"
                    f"<td>{plan(r)}</td><td>{e(status.get(t, ''))}</td>"
                    f"<td>{st.get('posts', 0)}<br><small>{ago(st.get('last_post', 0))}</small></td>"
                    f"<td{' style=color:#ff8a8a' if st.get('fails') else ''}>{st.get('fails', 0)}"
                    f"<br><small>{e(st.get('last_error', ''))}</small></td>"
                    f"<td><small>{time.strftime('%Y-%m-%d', time.localtime(r['created']))}</small></td></tr>")
    logs = "".join(f"<tr{' style=color:#ff8a8a' if lvl in ('WARNING', 'ERROR', 'CRITICAL') else ''}>"
                   f"<td><small>{time.strftime('%m-%d %H:%M:%S', time.localtime(ts))}</small></td><td><small>{lvl}</small></td>"
                   f"<td><small>{e(name)}: {e(msg)}</small></td></tr>"
                   for ts, lvl, name, msg in reversed(logbuf) if name not in ("httpx", "httpcore"))
    warn = sum(lvl in ("WARNING", "ERROR", "CRITICAL") for _, lvl, _, _ in logbuf)
    return ('<meta http-equiv="refresh" content="30"><style>main{max-width:1100px}td{vertical-align:top;border-top:1px solid #333}'
            'th{text-align:left}.network-incidents{border:1px solid var(--error);border-radius:22px;overflow-wrap:anywhere}'
            '.network-incidents p{margin:0 0 12px}.network-incidents ul{margin:0;padding:0;list-style:none}'
            '.network-incidents li span{margin-right:8px}'
            '.network-incidents li+li{margin-top:12px}.network-summary{color:var(--muted)}'
            '.admin-table{overflow-x:auto}.admin-table:focus-visible{outline:3px solid var(--focus)}</style>'
            + network_incidents() + '<h1>LiveChat XR admin</h1>' + network_totals() +
            f'<div class="box">{len(regs)} sign-ups: {sum(plan(r) == "founder" for r in regs.values())}/{FREE_SLOTS} founders, '
            f'{sum(plan(r) == "free" for r in regs.values())} free, {paid} paid, '
            f'{sum(len(live_channels(r)) < len(channels(r)) for r in regs.values())} waiting upgrade. {live} live right now. '
            f'{warn} warnings in the recent log.<br>{backup_line()} <small>Refreshes every 30 s.</small></div>'
            '<div class="admin-table" tabindex="0" role="region" aria-label="Relay accounts, scroll horizontally for all columns">'
            '<table cellpadding=6 width=100%><tr><th>User</th><th>Channel</th><th>Plan</th><th>Status</th><th>Posts</th>'
            f'<th>Failed posts</th><th>Joined</th></tr>{"".join(rows) or "<tr><td colspan=7>No sign-ups yet.</td></tr>"}</table></div>'
            '<h2>Recent log</h2><div class="admin-table" tabindex="0" role="region" aria-label="Recent log, scroll horizontally for all columns">'
            f'<table cellpadding=4 width=100%>{logs or "<tr><td>Empty.</td></tr>"}</table></div>')


def network_totals():
    try:
        if discovery is None or not discovery.store.path.exists(): raise ValueError
        demand = discovery.store.demand()
        with discovery.store.connect() as db:
            opted = db.execute("SELECT COUNT(DISTINCT discord_id) FROM listings WHERE opted_in=1 AND discord_id!=''").fetchone()[0]
        totals = f'Opt-ins: {opted} · Would pay: {demand["total"]} authenticated streamer(s).'
    except Exception:
        totals = "Opt-in and demand totals unavailable."
    return ('<section class="box" aria-label="Private discovery totals"><h2>Private discovery totals</h2><p>' + totals
            + '</p><p class="hint">Outside-cohort count unavailable — no verified cohort membership. '
              'E6 visitor measurement unavailable — no threshold passed. A demand tap is not a payment.</p></section>')


# Inline scripts/styles are part of the approved pages, so script-src keeps 'unsafe-inline'; the policy's value is the
# origin allowlist plus framing/base/object/form-action limits. form-action needs discord.com: Chrome applies it to the
# 303 that /register, /recover and the listing "link" action send to Discord's OAuth page.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' "
       "https://www.googletagmanager.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: "
       "https://*.google-analytics.com https://*.googletagmanager.com; connect-src 'self' "
       "https://*.google-analytics.com https://*.analytics.google.com https://*.googletagmanager.com; form-action 'self' https://discord.com; base-uri 'none'; "
       "object-src 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",  # ignored by browsers over plain HTTP (local dev)
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",  # manage links carry the token
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
RATE_LIMIT = (20, 600)  # /register and /recover per client IP per 10 minutes; each one fans out to Discord/TikTok/YouTube/Google
hits: dict[str, list[float]] = {}


def rate_limited(ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    limit, window = RATE_LIMIT
    with lock:
        if len(hits) > 10000:  # ponytail: drop idle IPs wholesale; a real store would expire per key
            for k in [k for k, v in hits.items() if v[-1] < now - window]:
                hits.pop(k)
        recent = [t for t in hits.get(ip, ()) if t > now - window]
        if len(recent) >= limit:
            hits[ip] = recent
            return True
        hits[ip] = recent + [now]
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveChatXR"
    timeout = 30  # a client that stalls headers or body must not hold a server thread forever

    def version_string(self) -> str:
        return self.server_version  # no Python version banner

    def end_headers(self) -> None:
        # Every response path (send(), /go/, /discord/interactions, stdlib send_error) passes through here.
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def client_ip(self) -> str:
        # Caddy appends the real client as the last X-Forwarded-For entry; the relay port is bound to the LAN only.
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        return forwarded or self.client_address[0]

    def send(self, code: int, body: bytes, location: str = "", ctype: str = "text/html; charset=utf-8", cookie: str = "") -> None:
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype)
        if ctype.startswith("text/javascript"):
            self.send_header("Cache-Control", "public, max-age=86400")
        elif self.path.startswith(("/m/", "/recover", "/network/", "/admin")):
            self.send_header("Cache-Control", "no-store")
        if code == 429:
            self.send_header("Retry-After", str(RATE_LIMIT[1]))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def same_browser(self, state: str) -> bool:
        """The OAuth state came back to the browser that started it (L1): its cookie holds the same value."""
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        held = jar[OAUTH_COOKIE].value if OAUTH_COOKIE in jar else ""
        return bool(held) and hmac.compare_digest(held.encode(), state.encode())

    def discord_redirect(self, scope: str, wanted: dict) -> None:
        """Start Discord OAuth. wanted = the channels to sign up; empty = log in only (scope identify, no webhook)."""
        state = secrets.token_urlsafe(24)
        with lock:
            oauth[state] = (time.time(), wanted)
        q = urllib.parse.urlencode({"client_id": DISCORD_ID, "response_type": "code", "scope": scope,
                                    "redirect_uri": f"{PUBLIC_URL}/discord/callback", "state": state})
        self.send(303, b"", location=f"https://discord.com/oauth2/authorize?{q}", cookie=state_cookie("/discord/callback", state))

    def discord_callback(self) -> None:
        q = {k: v[0] for k, v in parse_qs(self.path.partition("?")[2]).items()}
        state = q.get("state", "")
        with lock:
            for k in [k for k, (t, _) in oauth.items() if time.time() - t > 900]:
                oauth.pop(k)
            entry = oauth.pop(state, None)  # single use, even when the browser check below fails
        if not entry:
            return self.send(400, home(note(f"That Discord sign-in expired. Press {DISCORD_BUTTON} again.", True)))
        wanted = entry[1]
        if not self.same_browser(state):
            # Quest and phones often finish Discord's consent in Discord's own browser, which never saw our cookie.
            return self.send(400, home(note("Discord sign-in finished in a different browser from the one that started it. "
                                            f"Start again here, in this browser: press {DISCORD_BUTTON} below.", True), wanted))
        if "code" not in q:
            return self.send(400, home(note("Discord sign-in was cancelled. Try again when you're ready.", True), wanted))
        try:
            credential, me = discord_oauth(q["code"])
        except Exception as e:
            log.warning("discord oauth: %s", type(e).__name__)  # never the code or response
            return self.send(400, home(note("Discord didn't finish connecting. Please try again.", True), wanted))
        person = discord_person(me)
        if not wanted:  # log in
            with lock:
                mine = [(t, channel_label(r)) for t, r in regs.items() if r.get("discord_id") == person["id"]]
            if len(mine) == 1:
                return self.send(303, b"", location=f"/m/{mine[0][0]}")
            if mine:
                return self.send(200, relay_list(mine))
            return self.send(200, home(note("No relay is linked to this Discord account yet. Enter your channel, "
                                            f"then press {DISCORD_BUTTON}.")))
        return self.register({**wanted, "webhook": str((credential.get("webhook") or {}).get("url") or "")}, discord=person)

    def manage(self, token: str, msg: str = "", new: str = "", *, code: int = 200, form=None, network_errors=None, network_form=None) -> None:
        r = regs.get(token)
        if not r:
            return self.send(404, page(note("That link isn't active. It may have been deleted.", True) + '<p><a href="/">Start over</a></p>'))
        # Accounts from before channel links: fill in once, best effort. Never wait for the lock: register() shows
        # errors through this page while holding it.
        if not r.get("channel_id") and lock.acquire(blocking=False):
            try:
                hook = check_webhook(r["webhook"])
                r.update(guild_id=str(hook.get("guild_id") or ""), channel_id=str(hook.get("channel_id") or ""))
                save()
            except Exception:
                pass
            finally:
                lock.release()
        open_link = (f'https://discord.com/channels/{r["guild_id"]}/{r["channel_id"]}'
                     if r.get("guild_id") and r.get("channel_id") else "")
        billing = ""
        if DONATE_URL and plan(r) != "paid":
            billing = ('<div class="box"><b>Free: all your channels run.</b> LiveChat XR runs on donations. '
                       f'<a href="{html.escape(DONATE_URL)}">Support it</a> if it helps your streams.</div>')
        elif plan(r) == "free":
            q = f"?client_reference_id={token}&prefilled_email={quote(r['email'])}"
            links = " · ".join(f'<a href="{html.escape(url + q)}">{label}</a>'
                               for url, label in ((PAY_MONTHLY, "$3 / month"), (PAY_YEARLY, "$25 / year")) if url)
            billing = ('<div class="box"><b>Free plan: one channel live.</b> Extra channels stay saved until you upgrade. '
                       'After payment, refresh this page to check.<br>'
                       + (links or 'Upgrade links are not configured; your first channel stays live.') + '</div>')
        elif plan(r) == "founder":
            billing = '<div class="box"><b>Founder: all your channels free forever.</b> No payment needed.</div>'
        elif plan(r) == "paid" and BILLING_URL:
            billing = f'<p><a href="{html.escape(BILLING_URL)}">Manage billing or cancel</a> (sign in with {html.escape(r["email"])})</p>'
        listing = listing_section(token, r, network_errors, network_form)
        self.send(code, page(MANAGE.format(
            msg=msg + (note(r["channel_note"]) if r.get("channel_note") else ""), token=token,
            checklist=QUEST_CHECKLIST.format(token=token, open=" open" if new else "",
                                             channel=f'<a href="{open_link}" target="_blank" rel="noopener">the channel you connected</a>'
                                             if open_link else "the server and channel you connected",
                                             title="Last step: make chat pop up on your Quest" if new else "Quest pop-up checklist"),
            channels="".join(f'<div class="channel-row"><b>{LABEL[p]}:</b> {html.escape(c)}'
                             f'<div class="hint">{html.escape(channel_status(token, p))}</div></div>' for p, c in channels(r).items()),
            dname=html.escape(r["discord_channel"]) + (f' · <a href="{open_link}" target="_blank" rel="noopener">Open this channel in Discord</a>'
                                                         if open_link else ""),
            delivery=html.escape(delivery_status(token)),
            settings=settings_form(token, form if form is not None else {**r, **channels(r)}), billing=billing,
            login=' You can also <a href="/recover">log in with Discord</a>.' if r.get("discord_id") else "") + listing,
            ga="" if listing else "/m/", event=f"gtag('event','sign_up',{{method:'{new}'}});" if new in ("discord", "email") else "")
            .replace(b'</main>', (PROBE_STATUS.format(status=html.escape(status.get(token, "starting…"))) + '</main>').encode()))

    def do_GET(self):
        path = self.path.split("?")[0]
        # Exact allowlist only: never join request input onto a filesystem path.
        brand_files = {
            "/favicon.ico": ("favicon.ico", "image/vnd.microsoft.icon"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
            "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
            "/brand/header-dark.svg": ("header-dark.svg", "image/svg+xml"),
            "/brand/discord-avatar.png": ("discord-avatar.png", "image/png"),
        }
        if path in brand_files:
            name, mime = brand_files[path]
            root = Path(__file__).resolve().parent
            directory = root / "assets/brand/web" if (root / "assets/brand/web").is_dir() else root.parent / "assets/brand/web"
            try:
                data = (directory / name).read_bytes()
            except OSError:
                return self.send(404, b"Brand resource unavailable", ctype="text/plain; charset=utf-8")
            return self.send(200, data, ctype=mime)
        if path == "/network/discord/callback":
            return self.network_callback()
        if path.startswith("/go/"):
            opaque = path.removeprefix("/go/")
            destination = discovery.resolve_go(opaque) if discovery is not None and "?" not in self.path else None
            self.send_response(302 if destination else 404)
            self.send_header("Cache-Control", "no-store")
            if destination: self.send_header("Location", destination)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/":
            return self.send(200, home())
        if path == "/recover":
            return self.send(200, recovery_page())
        if path == "/admin" and ADMIN_KEY and secrets.compare_digest(parse_qs(self.path.partition("?")[2]).get("key", [""])[0], ADMIN_KEY):
            return self.send(200, page(admin_body()))
        if path == "/discord/callback":
            return self.discord_callback()
        if path == "/privacy":
            ga = ("Visits to these pages are counted with Google Analytics (pages viewed, rough location, device type)."
                  if GA_ID else "None.")
            return self.send(200, page(PRIVACY.format(ga=ga), ga="/privacy"))
        if path == "/terms":
            return self.send(200, page(TERMS, ga="/terms"))
        if path == "/pretext.js" and PRETEXT:
            return self.send(200, PRETEXT, ctype="text/javascript; charset=utf-8")
        if path == "/health":
            return self.send(200, f"ok {len(regs)}".encode())
        if m := re.fullmatch(r"/m/([\w-]{20,64})", path):
            q = parse_qs(self.path.partition("?")[2])
            msg = note("Settings saved. Your plan and private link are unchanged.") if q.get("saved") == ["1"] else ""
            if q.get("moved") == ["1"]:
                msg += note("You already had a relay for this stream. It now posts to the channel you just picked; nothing is duplicated.")
            return self.manage(m[1], msg=msg,
                               new=q.get("new", [""])[0])
        self.send(404, page(note("Not found.", True)))

    def do_POST(self):
        if self.path == "/discord/interactions":
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 < n <= 65536:
                code, result = 413, {}
            elif discovery is None:
                code, result = 503, {}
            else:
                code, result = discovery.interaction(self.headers, self.rfile.read(n))
            raw = json.dumps(result).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, b"invalid length")
        if n < 0: return self.send(400, b"invalid length")
        if self.path == "/stripe-webhook" and n <= 512 * 1024:
            return self.stripe(self.rfile.read(n))
        if n > 8192:  # every form here is a few short fields
            return self.send(413, page(note("Too much data.", True)))
        form = {k: v[0].strip() for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
        path = self.path.split("?")[0]
        # Browser form posts are same-origin only (CSRF defence in depth; manage links are capability URLs).
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return self.send(403, page(note("Request rejected. Open this page from the LiveChat XR site and try again.", True)))
        if path in ("/register", "/recover") and self.client_ip() not in ("127.0.0.1", "::1") and rate_limited(self.client_ip()):
            return self.send(429, page(note("Too many attempts from your network. Wait a few minutes and try again.", True)))
        if m := re.fullmatch(r"/m/([\w-]{20,64})/network", path):
            return self.network_post(m[1], form)
        if path == "/register":
            return self.register(form)
        if path == "/recover":  # Log in: identify only, so Discord creates no webhook
            if not discord_on():
                return self.send(400, recovery_page(note("Discord log-in is not configured here.", True)))
            return self.discord_redirect("identify", {})
        m = re.fullmatch(r"/m/([\w-]{20,64})/(test|delete|save|report)", path)
        if not m or m[1] not in regs:
            return self.send(404, page(note("That link isn't active.", True)))
        token, action = m[1], m[2]
        if action == "save":
            return self.register(form, token)
        if action == "report":
            try:
                saved = support_report(token)
            except OSError:
                return self.manage(token, note("Couldn't save the diagnostic. Please try again later.", True), code=503)
            return self.manage(token, note("Diagnostic saved for support. No private credentials or chat were included." if saved
                                           else "Your recent diagnostic is already saved. Please wait a minute before sending another."))
        if action == "test":
            r = regs[token]
            errors = []
            ok = chat.post_discord(r["webhook"], f"LiveChat XR: test message ✔ Chat from {r['channel']} will show up here.", on_error=errors.append)
            stats.setdefault(token, {})["discord_error"] = 0 if ok is not False else (errors[0] if errors else -1)
            return self.manage(token, note("Test sent. Check your Discord channel (and your Quest)." if ok is not False
                                           else "Discord didn't accept the test. Check the webhook below.", ok is False))
        with lock:
            r = regs.pop(token, None)
            stats.pop(token, None)
            save()
        stop(token)
        extra = ""
        if r and plan(r) == "paid" and BILLING_URL:
            extra = (f'<div class="box"><b>Your subscription is still active.</b> To stop billing, '
                     f'<a href="{html.escape(BILLING_URL)}">cancel it here</a> (sign in with {html.escape(r["email"])}).</div>')
        self.send(200, page(note("Stopped and deleted. Nothing more will be posted.") + extra + '<p><a href="/">Set up again</a></p>'))

    def network_callback(self):
        q = parse_qs(self.path.partition("?")[2])
        with lock:
            for key, (created, _) in list(network_oauth.items()):
                if time.monotonic() - created >= 900: network_oauth.pop(key, None)
            state = q.get("state", [""])[0]
            entry = network_oauth.pop(state, None)
            r = regs.get(entry[1]) if entry else None
            if (not entry or not NETWORK_WEB_PRIVATE or not r or not r.get("network_requested") or not q.get("code")
                    or not self.same_browser(state)):
                return self.send(400, page(note("Discord sign-in expired, was cancelled or finished in a different browser. "
                                                "Return to your private manage page in this browser and try again.", True)))
            csrf = network_csrf.get(entry[1])
        try:
            owner = discord_identify(q["code"][0])
            with lock:
                # Recheck after the HTTP exchange: removal/deletion while signing in wins.
                if (regs.get(entry[1]) is not r or not r.get("network_requested")
                        or csrf != network_csrf.get(entry[1])):
                    raise ValueError("registration changed")
                if not discovery or not discovery.enabled: raise ValueError("discovery unavailable")
                with discovery.store.guard:
                    old = listing_profile(r)
                    if r.get("network_owner") and r["network_owner"] != owner: raise ValueError("owner conflict")
                    if any(other is not r and other.get("network_owner") == owner for other in regs.values()):
                        raise ValueError("owner already linked")
                    profile = discovery.store.owner_listing(owner, create=True)
                    if old and old["id"] != profile["id"]: raise ValueError("listing conflict")
                    save_listing_association(r, network_listing=profile["id"], network_owner=owner)
                    discovery.store.set_listing(profile["id"], opted_in=True)
        except ValueError:
            return self.send(409, page(note("Discord identity conflicts with an existing association or setup changed. No accounts were merged. Return to your private manage page.", True)))
        except Exception:
            return self.send(503, page(note("Discord identity could not be linked. Return to your private manage page and try again.", True)))
        return self.send(303, b"", location=f"/m/{entry[1]}")

    def network_post(self, token, form):
        with lock:
            r = regs.get(token)
            action = form.get("action")
            removal = action == "optout" or (action == "consent" and form.get("listed") != "yes")
            if not r or (not NETWORK_WEB_PRIVATE and not (r.get("network_listing") and removal)):
                return self.send(404, page(note("Listing setup is not available.", True)))
            if (not PUBLIC_URL or self.headers.get("Origin") != PUBLIC_URL
                    or not network_csrf.get(token) or not secrets.compare_digest(form.get("csrf", ""), network_csrf[token])):
                return self.send(403, page(note("Request rejected. Reload your private manage page before trying again.", True)))
            allowed = {"consent": {"listed"}, "optout": set(), "link": set(),
                       "profile": {"ign", "timezone", "other_timezone"}, "prove": set()}
            if action not in allowed or not form.keys() <= allowed[action] | {"action", "csrf"}:
                return self.manage(token, note("Invalid listing action.", True), code=400)
            if not discovery or not discovery.store.path.exists():
                return self.manage(token, note("Listing storage is unavailable. Nothing was confirmed; try again.", True), code=503)
            try:
                profile = listing_profile(r)
                if removal:
                    save_listing_association(r, network_requested=False)
                    network_csrf[token] = secrets.token_urlsafe(32)  # Invalidates an already-exchanging callback too.
                    for key, (_, owner_token) in list(network_oauth.items()):
                        if owner_token == token: network_oauth.pop(key, None)
                elif action == "consent":
                    save_listing_association(r, network_requested=True)
                    if profile: discovery.store.set_listing(profile["id"], opted_in=True)
                elif action == "link":
                    if not r.get("network_requested") or not DISCORD_ID or not DISCORD_SECRET or not discovery.enabled:
                        return self.manage(token, note("Save listing consent first; Discord identity and the private network must be configured.", True), code=409)
                    for key, (created, owner_token) in list(network_oauth.items()):
                        if owner_token == token or time.monotonic() - created >= 900: network_oauth.pop(key, None)
                    if len(network_oauth) >= MAX_REGS: raise ValueError("too many sign-ins")
                    state = secrets.token_urlsafe(32)
                    network_oauth[state] = (time.monotonic(), token)
                    q = urllib.parse.urlencode(dict(client_id=DISCORD_ID, response_type="code", scope="identify",
                        redirect_uri=f"{PUBLIC_URL}/network/discord/callback", state=state))
                    return self.send(303, b"", location="https://discord.com/oauth2/authorize?" + q,
                                     cookie=state_cookie("/network/discord/callback", state))
                else:
                    if not profile or not profile["opted_in"] or not r.get("network_requested"):
                        return self.manage(token, note("Save listing consent and link your Discord identity first.", True), code=409)
                    if action == "profile":
                        zone = form.get("other_timezone", "") if form.get("timezone") == "Other" else form.get("timezone", "")
                        try:
                            discovery.store.update_profile(profile["id"], ign=form.get("ign", ""), timezone=zone)
                        except ValueError as error:
                            field = "ign" if str(error).startswith("IGN") else "timezone"
                            return self.manage(token, code=400, network_errors={field: str(error)}, network_form=form)
                    else:
                        return self.manage(token, note("Use /optin in the private Discord server with your linked account. Channel proof is checked there, not by this page."))
            except ValueError:
                return self.manage(token, note("Listing ownership or state conflicts. No account was taken over.", True), code=409)
            except Exception:
                return self.manage(token, note("Listing change could not be confirmed. Reload and try again.", True), code=503)
        if removal and profile:
            if not loop.is_running():
                discovery.store.set_listing(profile["id"], opted_in=False)
                return self.manage(token, note("Removed locally. Relay loop is stopped; remote edits are not confirmed.", True), code=503)
            future = asyncio.run_coroutine_threadsafe(discovery.remove_listing(profile["id"]), loop)
            try:
                okay = future.result(timeout=10)
            except TimeoutError:
                # Do not cancel the shared removal halfway through remote reconciliation.
                return self.manage(token, note("Removal is still pending. Reload to check; remote edits are not confirmed."), code=202)
            except Exception:
                return self.manage(token, note("Removal could not be confirmed. Reload and try Remove my listing now again.", True), code=503)
            return self.manage(token, note(network.REMOVED if okay else "Removed locally. Some remote edits failed; bounded reconciliation is queued."))
        return self.manage(token, note("Listing consent saved." if action == "consent" else "Listing profile saved."))

    def stripe(self, body: bytes) -> None:
        ev = stripe_event(body, self.headers.get("Stripe-Signature", ""))
        if ev is None:
            return self.send(400, b"bad signature")
        obj = ev.get("data", {}).get("object", {})
        if ev.get("type") == "checkout.session.completed" and obj.get("payment_status") == "paid":
            token = obj.get("client_reference_id") or ""
            with lock:
                r = regs.get(token)
                if r and plan(r) != "founder":
                    r.update(plan="paid", subscription=obj.get("subscription"))
                    save()
            if r and plan(r) == "paid":
                start(token)
                chat.post_discord(r["webhook"], "LiveChat XR: payment received ✔ It's on. Chat shows up here while you're live.")
        elif ev.get("type") == "customer.subscription.deleted":
            with lock:
                token = next((t for t, r in regs.items() if obj.get("id") and r.get("subscription") == obj["id"]
                              and plan(r) == "paid"), None)
                if token:
                    regs[token]["plan"] = "free"
                    save()
            if token:
                start(token)
                message = ("All your saved channels stay free." if DONATE_URL else
                           "Your first channel stays free; extra channels are saved, waiting for upgrade. "
                           f"Renew here: <{manage_url(token)}>")
                chat.post_discord(regs[token]["webhook"], "LiveChat XR: your subscription ended. " + message)
        self.send(200, b"ok")

    def register(self, form: dict, edit_token: str = "", discord: dict | None = None) -> None:
        """New sign-up, settings save (edit_token) or the Discord callback (discord = the verified Discord identity)."""
        def fail(message, code=400):
            if edit_token:
                return self.manage(edit_token, note(message, True), code=code, form=form)
            return self.send(code, home(note(message, True), form))
        wanted = {p: normalise_channel(p, form[p]) for p in LABEL if form.get(p, "").strip()}
        if not wanted and form.get("platform") in LABEL and form.get("channel", "").strip():  # older single-platform form
            wanted[form["platform"]] = normalise_channel(form["platform"], form["channel"])
        webhook = form.get("webhook", "") or (regs[edit_token]["webhook"] if edit_token else "")
        # With Discord OAuth configured, every new registration comes back through the callback with a Discord identity;
        # a posted webhook alone can't create one (M2).
        via_discord = not edit_token and not discord and discord_on()
        if via_discord and not wanted:
            return self.discord_redirect("identify", {})  # no channels typed: log in
        if discord:
            form.update(name=discord["name"], email=discord["email"], signin="discord")
        name, email = form.get("name", "")[:60], form.get("email", "").lower()
        linked = bool(discord) or bool(edit_token and regs[edit_token].get("discord_id"))  # Discord may share no email
        err = None
        if not via_discord and (not name or ((email or not linked) and not EMAIL.match(email))):
            err = "Please enter your name and a valid email."
        elif not wanted:
            err = "Enter your TikTok handle, your Twitch channel, your YouTube handle, or any mix."
        elif any(not CHANNEL[p].match(c) for p, c in wanted.items()):
            err = "That channel name doesn't look right."
        if err:
            return fail(err)
        channel_note = ""
        for p, c in wanted.items():
            try:
                exists = check_channel(p, c)
            except Exception as e:
                log.warning("channel check: %s", type(e).__name__)  # no credentials or remote response in logs
                exists = None
            if exists is False:
                return fail(f"We can't find that {LABEL[p]} account. Check the spelling, without the @")
            if exists is None:
                channel_note = "We couldn't verify your account right now. You're signed up; check the spelling if chat doesn't connect."
        if via_discord:  # one consent: who you are, plus a webhook in the channel you pick
            return self.discord_redirect("identify email webhook.incoming", wanted)
        if not WEBHOOK.match(webhook):
            err = "That isn't a Discord webhook URL. It should start with https://discord.com/api/webhooks/"
        if err:
            return fail(err)
        try:
            hook = check_webhook(webhook)
        except Exception:
            return fail("Discord didn't accept that webhook. Copy it again from the channel's Integrations page.")
        dname, guild = hook.get("name") or "webhook", str(hook.get("guild_id") or "")
        moved_from = ""
        with lock:
            owner = next((t for t, r in regs.items() if r["webhook"] == webhook), None)
            if edit_token and owner and owner != edit_token:
                return fail("That webhook is already connected to another relay. Use a different webhook.")
            did = discord["id"] if discord else ""
            mine = [t for t, r in regs.items() if did and r.get("discord_id") == did]
            if not edit_token and not owner:
                # The same Discord account signing up the same stream again moves that relay to the channel just picked.
                owner = next((t for t in mine if same_channels(channels(regs[t]), wanted)), None)
            if not edit_token and not owner and guild:
                # Signing up again for the same stream in the same Discord server replaces the earlier relay instead of
                # running a duplicate. Keyed by server, not email: only someone who manages that server can make its webhook.
                # This is also how a relay from before Discord sign-in gets linked to a Discord account.
                owner = next((t for t, r in regs.items() if r.get("guild_id") == guild and same_channels(channels(r), wanted)), None)
                if owner and did and regs[owner].get("discord_id", did) != did:
                    return fail("This stream already posts into this Discord server from another Discord account. "
                                "Ask them to remove it, or pick a channel in a different server.", 409)
            if not edit_token and not owner and len(mine) >= REGS_PER_DISCORD_USER:
                return fail(f"This Discord account already has {REGS_PER_DISCORD_USER} relays. Log in and edit one of them, "
                            "or delete one first.", 409)
            if not edit_token and owner and regs[owner]["webhook"] != webhook:
                moved_from = regs[owner]["webhook"]
            token = edit_token or owner
            if token is None and len(regs) >= MAX_REGS:
                return fail("This server is full right now. Try again later.", 503)
            old = regs.get(token, {}) if token else {}
            email = email or (old.get("email", "") if discord else "")
            token = token or secrets.token_urlsafe(24)
            p = plan(old) if old else ("founder" if free_used() < FREE_SLOTS else "free")
            if p == "founder" and token not in founders:
                founders.add(token)
                write_json("founders.json", sorted(founders))
            platform, channel = next(iter(wanted.items()))  # primary pair kept for older readers (admin exports, QA tools)
            regs[token] = {**old, "name": name, "email": email, "platform": platform, "channel": channel, "channels": wanted, "webhook": webhook,
                           "discord_channel": dname, "guild_id": guild, "channel_id": str(hook.get("channel_id") or ""),
                           "created": old.get("created", int(time.time())), "plan": p,
                           "signin": form.get("signin") or old.get("signin", "email"), "channel_note": channel_note,
                           "platform_icons": bool(form.get("platform_icons")) if form.get("icons_form") else old.get("platform_icons", True),
                           **({"discord_id": did, "discord_username": discord["username"]} if discord else {})}
            regs[token]["identities"] = {p: ident for p, ident in old.get("identities", {}).items()
                                          if p in wanted and channels(old).get(p) == wanted[p]}
            if edit_token:
                stats.pop(token, None)
            save()
        resolve_saved_identities(token)
        start(token)
        active = " · ".join(f"{LABEL[p]} {c}" for p, c in live_channels(regs[token]).items())
        waiting = " Extra channels are saved, waiting for upgrade." if len(wanted) > len(live_channels(regs[token])) else ""
        chat.post_discord(webhook, f"LiveChat XR connected ✔ Chat from {active} will show up here while you're live.{waiting}\n"
                                   f"Manage or stop it: <{manage_url(token)}>")
        if moved_from and moved_from != webhook:  # the audit trail stays in the old channel, without the private link
            chat.post_discord(moved_from, f"LiveChat XR: chat from {active} moved to another Discord channel after a new sign-up.")
        self.send(303, b"", location=f"/m/{token}?saved=1" if edit_token else
                  f"/m/{token}?new={'discord' if discord else 'email'}" + ("&moved=1" if moved_from else ""))

    def log_message(self, fmt, *args):  # keep tokens and query strings (admin key, OAuth codes) out of logs
        # A timed-out or malformed request is logged before parse_request sets path/command.
        path, command = getattr(self, "path", "-"), getattr(self, "command", None) or "-"
        if path.startswith("/go/"): return  # No redirect click trail or visitor measurement.
        path = re.sub(r"/m/[\w-]+", "/m/…", path.split("?")[0]).replace("\r", "").replace("\n", "").replace("\t", " ")
        log.info("%s %s", command, path)  # relay.log is tab/line-separated; the request line is client input


def main() -> None:
    global discovery
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    DATA.mkdir(parents=True, exist_ok=True)
    load_log_tail()
    try:
        stats.update(json.loads((DATA / "stats.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    filelog = logging.handlers.RotatingFileHandler(DATA / "relay.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    filelog.setFormatter(logging.Formatter("%(created)f\t%(levelname)s\t%(name)s\t%(message)s"))
    filelog.addFilter(lambda rec: rec.name not in ("httpx", "httpcore"))
    for h in (filelog, _Ring(logging.INFO)):
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)
    loop.create_task(save_stats_loop())
    discovery = network.Network(DATA, normalise_channel)
    loop.create_task(discovery.start())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # TikTokLive logs every request URL
    load_registrations()
    for i, token in enumerate(regs):
        start(token, delay=i * 3)
        # Enrich even upgrade-waiting channels, without delaying HTTP health checks or relays.
        threading.Thread(target=resolve_saved_identities, args=(token,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("listening on :%d with %d registrations", PORT, len(regs))
    if DISCORD_ID and not PUBLIC_URL:
        log.error("DISCORD_CLIENT_ID is set but PUBLIC_URL is empty: Discord sign-up is off (typed form shown) until PUBLIC_URL is in the env file")
    previous_term = signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(loop.stop))
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(discovery.stop())
        srv.shutdown()
        srv.server_close()
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    main()

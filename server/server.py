"""LiveChat XR relay server: people enter their TikTok/Twitch channel and a Discord webhook; the server
posts their stream chat to that Discord channel. The Discord app on a standalone Quest pops it up in-game.

Billing: the first FREE_SLOTS sign-ups are free. Later sign-ups are saved as "pending" and start only
after Stripe reports a paid checkout for them (Payment Link + client_reference_id = their token, verified
via the signed /stripe-webhook). A cancelled subscription stops the relay again.

No accounts: the webhook URL is the credential (whoever has it can already post there). Registering the
same webhook again updates it. Each registration gets a private manage link, which is also posted into
their Discord channel so it can't get lost.

    DATA_DIR=./data PORT=13300 python server.py      (needs app/chat.py next to it or on PYTHONPATH)
"""
import asyncio
import collections
import hashlib
import hmac
import html
import json
import logging
import logging.handlers
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote

import chat

DATA = Path(os.environ.get("DATA_DIR", "data"))
PORT = int(os.environ.get("PORT", "13300"))
MAX_REGS = int(os.environ.get("MAX_REGS", "50"))  # hard ceiling, paid or not (one server, one IP)
FREE_SLOTS = int(os.environ.get("FREE_SLOTS", "5"))
PAY_MONTHLY = os.environ.get("PAY_MONTHLY", "")  # Stripe Payment Link URLs
PAY_YEARLY = os.environ.get("PAY_YEARLY", "")
BILLING_URL = os.environ.get("BILLING_URL", "")  # Stripe customer portal login link (manage / cancel)
STRIPE_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DISCORD_ID = os.environ.get("DISCORD_CLIENT_ID", "")  # one-click "Connect Discord" (OAuth scope webhook.incoming)
DISCORD_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # /admin?key=... lists who is signed up; unset = no admin page
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
GA_ID = os.environ.get("GA_ID", "")  # Google Analytics 4 measurement ID (G-...); unset = no analytics
GOOGLE_ID = os.environ.get("GOOGLE_CLIENT_ID", "")  # optional "Continue with Google" fills in a verified name + email
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{15,22}/[\w-]{40,100}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
CHANNEL = {"tiktok": re.compile(r"^@?[A-Za-z0-9_.]{2,24}$"), "twitch": re.compile(r"^#?[A-Za-z0-9_]{3,25}$"),
           "youtube": re.compile(r"^@?[A-Za-z0-9_.-]{3,30}$")}
LABEL = {"tiktok": "TikTok", "twitch": "Twitch", "youtube": "YouTube"}
log = logging.getLogger("relay")

regs: dict[str, dict] = {}      # token -> {name, email, platform, channel, webhook, discord_channel, created,
                                #           plan: free|paid|pending, subscription}
status: dict[str, str] = {}     # token -> latest chat status line
tasks: dict[str, asyncio.Task] = {}
stats: dict[str, dict] = {}     # token -> {posts, fails, last_post, last_error}; saved to stats.json every 30 s
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

lock = threading.Lock()
oauth: dict[str, tuple[float, dict]] = {}  # state -> (created, sign-up form) while the user is on Discord
loop = asyncio.new_event_loop()


def save() -> None:
    write_json("registrations.json", regs)


def channels(r: dict) -> dict[str, str]:
    """{platform: channel} for a registration; pre-0.2 records only have the single platform/channel pair."""
    return r.get("channels") or {r["platform"]: r["channel"]}


def channel_label(r: dict) -> str:
    return " · ".join(f"{LABEL[p]} {c}" for p, c in channels(r).items())


def plan(r: dict) -> str:
    return r.get("plan", "free")  # registrations from before billing existed were free


def free_used() -> int:
    return sum(plan(r) == "free" for r in regs.values())


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


def check_webhook(url: str) -> str:
    """Returns the webhook's display name; raises if Discord doesn't know it. Only Discord URLs get here."""
    req = urllib.request.Request(url, headers={"User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("name") or "webhook"


def normalise_channel(platform: str, channel: str) -> str:
    channel = channel.strip()
    if channel.startswith(("https://", "http://")):
        try:
            url = urllib.parse.urlparse(channel)
        except ValueError:
            return channel  # malformed URLs fail the existing channel syntax check
        hosts = {"tiktok": ("tiktok.com", "www.tiktok.com", "m.tiktok.com"),
                 "twitch": ("twitch.tv", "www.twitch.tv", "m.twitch.tv"),
                 "youtube": ("youtube.com", "www.youtube.com", "m.youtube.com")}
        if url.hostname in hosts.get(platform, ()):
            channel = url.path.strip("/").split("/")[0]  # also drops a trailing /live
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


async def run(token: str, delay: float = 0) -> None:
    await asyncio.sleep(delay)  # stagger startup: TikTok's sign server rate-limits connects per IP
    r = regs[token]
    tag_pending = False

    def on_connect() -> None:
        nonlocal tag_pending
        tag_pending = True

    def post(text: str) -> None:
        ok = chat.post_discord(r["webhook"], text) is not False
        st = stats.setdefault(token, {"posts": 0, "fails": 0, "last_post": 0, "last_error": ""})
        st["posts" if ok else "fails"] += 1
        if ok:
            st["last_post"] = time.time()
        else:
            st["last_error"] = time.strftime("%m-%d %H:%M") + " Discord post failed"

    def on_batch(text: str) -> None:
        nonlocal tag_pending
        if tag_pending and plan(r) == "free":
            tag = "\nvia LiveChat XR - livechat.deliciouswines.org"
            text = text[:2000 - len(tag)] + tag
        tag_pending = False
        threading.Thread(target=post, args=(text,), daemon=True).start()

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
        try:
            post(chat.session_summary(row))
        except Exception:
            log.warning("stream stats notification failed")

    def on_session(row: dict) -> None:
        threading.Thread(target=save_session, args=(row,), daemon=True).start()

    while True:
        try:
            await chat.relay(r["platform"], r["channel"], on_batch, on_status, channels=channels(r),
                             on_connect=on_connect, on_session=on_session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            on_status(f"error, retrying ({type(e).__name__})")
            log.warning("%s: %r", channel_label(r), e)
            await asyncio.sleep(60)


def start(token: str, delay: float = 0) -> None:
    def go():
        if t := tasks.pop(token, None):
            t.cancel()
        status[token] = "starting…"
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
.site nav a{color:var(--label);text-decoration:none;margin-left:18px}
main{max-width:600px;margin:0 auto;padding:20px 20px 60px}
h1{font-size:32px;line-height:36px;font-weight:800;margin:0 0 14px}
h2{font-size:19px;line-height:24px;font-weight:800;margin:6px 0 12px}
small,.hint{font-size:13px;color:var(--label)}
code{font-size:.9em}
.box{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin:16px 0;text-align:left}
.box.err{background:var(--error);border-color:#7a2a38}
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
.card{margin-top:28px;background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:30px}
.step{display:grid;grid-template-columns:38px 1fr;gap:16px;padding-bottom:26px;position:relative}
.step:last-child{padding-bottom:0}
.step:not(:last-child)::before{content:"";position:absolute;left:18px;top:42px;bottom:4px;width:2px;background:var(--line)}
.n{width:38px;height:38px;border-radius:12px;background:var(--accent);color:#1a1300;display:grid;place-items:center;font-weight:800;font-size:17px}
.step.later .n{background:var(--line);color:var(--accent)}
.step p{margin:0;color:var(--text-2)}
label{display:block;font-size:13px;font-weight:600;color:var(--label);margin:12px 0 6px}
input[type=text],input[type=email],input[type=url],input:not([type]){width:100%;padding:12px 14px;font:inherit;color:var(--text);
border:1px solid var(--line-field);border-radius:12px;background:var(--bg)}
input::placeholder{color:var(--placeholder)}
input:focus{outline:3px solid #8b5cf655;border-color:var(--focus)}
input[readonly]{color:var(--muted)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.gsi{margin:12px 0 4px;min-height:44px}
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
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&display=swap" rel="stylesheet">
<style>{css}</style>{head}</head><body>
<header class="site"><a class="brand" href="/">LiveChat XR</a>
<nav aria-label="Site"><a href="https://github.com/DeliciousHouse/livechat-xr">GitHub</a><a href="/privacy">Privacy</a></nav></header>
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
<span class="chip">First spots free · then $3/month</span>
<p class="hint">Free posts carry a one-line tag; paid posts do not.</p></div>
{msg}
<form class="card" method="post" action="/register" aria-label="Set up LiveChat XR for Discord">
<section class="step" aria-labelledby="s1"><div class="n" aria-hidden="true">1</div><div><h2 id="s1">Your channel</h2>
<label for="tiktok">TikTok handle</label><input type="text" id="tiktok" name="tiktok" value="{tiktok}" placeholder="@yourhandle or profile URL"
maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="twitch">Twitch channel</label><input type="text" id="twitch" name="twitch" value="{twitch}" placeholder="yourchannel or twitch.tv URL"
maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="youtube">YouTube handle</label><input type="text" id="youtube" name="youtube" value="{youtube}" placeholder="@yourhandle or youtube.com URL"
maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="hint">Fill in any of them: chat from all of them lands in the same Discord channel.</div>
{google}<div class="two"><div><label for="name">Name</label><input type="text" id="name" name="name" value="{name}" required maxlength="60" autocomplete="name"></div>
<div><label for="email">Email</label><input type="email" id="email" name="email" value="{email}" required maxlength="200" autocomplete="email"></div></div>
</div></section>
<section class="step" aria-labelledby="s2"><div class="n" aria-hidden="true">2</div><div><h2 id="s2">Connect Discord</h2>
{discord}{manual_help}
<li>In Discord, open a server you own (a new private one is fine) and make a channel like <code>#stream-chat</code>.</li>
<li>Channel settings (gear) → <b>Integrations</b> → <b>Webhooks</b> → <b>New Webhook</b> → <b>Copy Webhook URL</b>.</li>
<li>Paste it above and press Connect. You'll get a test message in that channel.</li></ol></details>
</div></section>
<section class="step later" aria-labelledby="s3"><div class="n" aria-hidden="true">3</div><div><h2 id="s3">On your Quest</h2>
<p data-pretext>Install Discord on your Quest if you have not already, open the channel, and set notifications to <b>All Messages</b>.</p></div></section>
</form>
<footer>Your name and email are only used to know who is using it.{ga_note} Your webhook URL is only used to post your chat.
Mentions are disabled, so chat can't ping anyone.<br><a href="https://github.com/DeliciousHouse/livechat-xr">Open source on GitHub</a> ·
<a href="/privacy">Privacy</a></footer>"""

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
<p>LiveChat XR for Discord posts your TikTok LIVE, Twitch or YouTube chat into a Discord channel you choose. This is everything it keeps.</p>
<div class="box"><b>What we store</b><ul>
<li>Your name and email: typed in, or taken from your Google account if you use Continue with Google
(only your name and email address; nothing else from Google).</li>
<li>Your TikTok, Twitch or YouTube channel name and the Discord webhook for your channel.</li>
<li>Counts of messages posted and failed, for troubleshooting. Chat messages are passed straight to Discord, not stored.</li>
<li>If you pay, Stripe handles the payment; we only keep the subscription ID.</li></ul></div>
<div class="box"><b>What we don't do</b><ul><li>We don't sell or share your details. They are only used to run the relay and to
know who is using it.</li><li>We don't post anywhere except the Discord channel you connected.</li></ul></div>
<div class="box"><b>Analytics</b><br>{ga}</div>
<div class="box"><b>Fonts</b><br>The page font is loaded from Google Fonts, so your browser fetches it from Google's servers.</div>
<div class="box"><b>Deleting your data</b><br>Use <b>Stop and delete</b> on your private manage page (the link is also
posted in your Discord channel). That removes your registration right away. Backup copies roll off within 90 days.
Questions: <a href="https://github.com/DeliciousHouse/livechat-xr/issues">open an issue on GitHub</a>.</div>
<p><a href="/">Back</a></p>"""

MANAGE = """<h1>Your chat relay</h1>{msg}
<div class="box">{channels}<br><b>Discord:</b> webhook “{dname}”<br><b>Status:</b> {status}</div>{billing}
<p>Keep this page's link; it's also posted in your Discord channel. Leave it running: it picks up your chat
whenever you go live.</p>
<div class="actions"><form class="inline" method="post" action="/m/{token}/test"><button>Send test message</button></form>
<form class="inline" method="post" action="/m/{token}/delete" onsubmit="return confirm('Stop posting chat to Discord?')">
<button class="alt">Stop and delete</button></form></div>
<p><a href="/">Set up another channel</a></p>"""


WEBHOOK_FIELD = ('<label for="webhook">Discord webhook URL</label><input type="url" id="webhook" name="webhook" '
                 'placeholder="https://discord.com/api/webhooks/…" value="{webhook}" maxlength="200"{req}>')


def home(msg: str = "", form: dict | None = None) -> bytes:
    form = form or {}
    chans = {p: form.get(p, "") for p in LABEL}
    if form.get("platform") in LABEL and form.get("channel"):  # older single-platform form
        chans[form["platform"]] = form["channel"]
    values = {k: html.escape(form.get(k, "")) for k in ("name", "email", "webhook")} | {p: html.escape(c) for p, c in chans.items()}
    if DISCORD_ID:
        discord = ('<button class="cta" name="via" value="discord">Connect Discord</button><p class="hint">Discord asks which server '
                   'and channel to post in. Pick a channel in a server you own, e.g. a new <code>#stream-chat</code>.</p>'
                   '<details><summary>or paste a webhook URL</summary>'
                   + WEBHOOK_FIELD.format(req="", webhook=values["webhook"])
                   + '<button class="cta alt" name="via" value="webhook">Connect with webhook</button></details>')
        manual = '<details><summary>Getting a webhook URL by hand</summary><ol>'
    else:
        discord = WEBHOOK_FIELD.format(req=" required", webhook=values["webhook"]) + '<button class="cta" style="margin-top:14px">Connect</button>'
        manual = '<details open><summary>Getting a webhook URL (1 minute)</summary><ol>'
    google = ""
    if GOOGLE_ID:  # Google Identity Services: the ID token rides along in the form and is verified in register()
        google = (f'<script src="https://accounts.google.com/gsi/client" async></script><div id="g_id_onload" '
                  f'data-client_id="{html.escape(GOOGLE_ID)}" data-callback="gsi" data-auto_prompt="false"></div>'
                  '<div class="gsi"><div class="g_id_signin" data-type="standard" data-theme="filled_black" data-shape="pill" '
                  'data-text="continue_with"></div></div><input type="hidden" name="google" id="gcred">'
                  '<p id="gwho" class="hint">Or type your name and email:</p><script>function gsi(r){'
                  'var p=JSON.parse(decodeURIComponent(escape(atob(r.credential.split(".")[1].replace(/-/g,"+").replace(/_/g,"/")))));'
                  'gcred.value=r.credential;name.value=p.name||p.email;email.value=p.email;name.readOnly=email.readOnly=true;'
                  'gwho.textContent="Signed in with Google ✔"}</script>')
    ga_note = " Visits are counted with Google Analytics." if GA_ID else ""
    return page(HOME.format(msg=msg, discord=discord, manual_help=manual, google=google, ga_note=ga_note, icon=DISCORD_ICON,
                            **values) + PRETEXT_JS, ga="/")


def google_identity(credential: str) -> tuple[str, str]:
    """Verify a Google ID token with Google's tokeninfo endpoint; returns (name, email). Raises if it doesn't check out."""
    url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": credential})
    with urllib.request.urlopen(url, timeout=10) as r:
        t = json.load(r)
    if (t.get("aud") != GOOGLE_ID or t.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
            or t.get("email_verified") not in ("true", True) or int(t.get("exp", 0)) < time.time()):
        raise ValueError("Google token rejected")
    return (t.get("name") or t["email"])[:60], t["email"].lower()


def discord_exchange(code: str) -> str:
    """OAuth code -> the webhook URL Discord created in the channel the user picked."""
    body = urllib.parse.urlencode({"client_id": DISCORD_ID, "client_secret": DISCORD_SECRET, "grant_type": "authorization_code",
                                   "code": code, "redirect_uri": f"{PUBLIC_URL}/discord/callback"}).encode()
    req = urllib.request.Request("https://discord.com/api/oauth2/token", body,
                                 {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["webhook"]["url"]


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


def admin_body() -> str:
    e = html.escape
    live = sum("connected" in status.get(t, "") for t in regs)
    paid = sum(plan(r) == "paid" for r in regs.values())
    rows = []
    for t, r in sorted(regs.items(), key=lambda kv: -kv[1]["created"]):
        st = stats.get(t, {})
        g = " (Google)" if r.get("signin") == "google" else ""
        rows.append(f"<tr><td>{e(r.get('name', ''))}<br><small>{e(r.get('email', ''))}{g}</small></td>"
                    f"<td>{e(channel_label(r))}<br><small>Discord: {e(r['discord_channel'])}</small></td>"
                    f"<td>{plan(r)}</td><td>{e(status.get(t, 'stopped' if plan(r) == 'pending' else ''))}</td>"
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
            'th{text-align:left}</style><h1>LiveChat XR admin</h1>'
            f'<div class="box">{len(regs)} sign-ups: {free_used()}/{FREE_SLOTS} free, {paid} paid, '
            f'{sum(plan(r) == "pending" for r in regs.values())} waiting for payment. {live} live right now. '
            f'{warn} warnings in the recent log.<br>{backup_line()} <small>Refreshes every 30 s.</small></div>'
            '<table cellpadding=6 width=100%><tr><th>User</th><th>Channel</th><th>Plan</th><th>Status</th><th>Posts</th>'
            f'<th>Failed posts</th><th>Joined</th></tr>{"".join(rows) or "<tr><td colspan=7>No sign-ups yet.</td></tr>"}</table>'
            f'<h2>Recent log</h2><table cellpadding=4 width=100%>{logs or "<tr><td>Empty.</td></tr>"}</table>')


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveChatXR"

    def send(self, code: int, body: bytes, location: str = "", ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        if ctype.startswith("text/javascript"):
            self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")  # manage links carry the token
        self.end_headers()
        self.wfile.write(body)

    def manage(self, token: str, msg: str = "", new: str = "") -> None:
        r = regs.get(token)
        if not r:
            return self.send(404, page(note("That link isn't active. It may have been deleted.", True) + '<p><a href="/">Start over</a></p>'))
        billing, st = "", status.get(token, "starting…")
        if "TikTok: can't find" in st:
            msg += note("To correct your channel, use Set up another channel below with the same Discord webhook. "
                        "This updates your existing registration; no need to delete it.")
        if plan(r) == "pending":
            st = "waiting for payment"
            q = f"?client_reference_id={token}&prefilled_email={quote(r['email'])}"
            billing = ('<div class="box"><b>The free spots are taken.</b> Pick a plan to switch it on. It starts within a '
                       'minute of paying; refresh this page to check.<br>'
                       f'<a href="{html.escape(PAY_MONTHLY + q)}"><button>$3 / month</button></a> '
                       f'<a href="{html.escape(PAY_YEARLY + q)}"><button>$25 / year</button></a></div>')
        elif plan(r) == "paid" and BILLING_URL:
            billing = f'<p><a href="{html.escape(BILLING_URL)}">Manage billing or cancel</a> (sign in with {html.escape(r["email"])})</p>'
        self.send(200, page(MANAGE.format(
            msg=msg + (note(r["channel_note"]) if r.get("channel_note") else ""), token=token,
            channels="<br>".join(f"<b>{LABEL[p]}:</b> {html.escape(c)}" for p, c in channels(r).items()),
            dname=html.escape(r["discord_channel"]),
            status=html.escape(st), billing=billing),
            ga="/m/", event=f"gtag('event','sign_up',{{method:'{new}'}});" if new in ("google", "email") else ""))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(200, home())
        if path == "/admin" and ADMIN_KEY and secrets.compare_digest(parse_qs(self.path.partition("?")[2]).get("key", [""])[0], ADMIN_KEY):
            return self.send(200, page(admin_body()))
        if path == "/discord/callback":
            q = {k: v[0] for k, v in parse_qs(self.path.partition("?")[2]).items()}
            with lock:
                for k in [k for k, (t, _) in oauth.items() if time.time() - t > 900]:
                    oauth.pop(k)
                entry = oauth.pop(q.get("state", ""), None)
            if not entry:
                return self.send(400, home(note("That Discord sign-in expired. Please fill in the form again.", True)))
            if "code" not in q:
                return self.send(400, home(note("Discord connection was cancelled. Try again, or paste a webhook URL instead.", True)))
            try:
                webhook = discord_exchange(q["code"])
            except Exception as e:
                log.warning("discord oauth: %r", e)
                return self.send(400, home(note("Discord didn't finish connecting. Please try again.", True)))
            return self.register({**entry[1], "webhook": webhook, "via": "webhook"})
        if path == "/privacy":
            ga = ("Visits to these pages are counted with Google Analytics (pages viewed, rough location, device type)."
                  if GA_ID else "None.")
            return self.send(200, page(PRIVACY.format(ga=ga), ga="/privacy"))
        if path == "/pretext.js" and PRETEXT:
            return self.send(200, PRETEXT, ctype="text/javascript; charset=utf-8")
        if path == "/health":
            return self.send(200, f"ok {len(regs)}".encode())
        if m := re.fullmatch(r"/m/([\w-]{20,64})", path):
            return self.manage(m[1], new=parse_qs(self.path.partition("?")[2]).get("new", [""])[0])
        self.send(404, page(note("Not found.", True)))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if self.path == "/stripe-webhook" and n <= 512 * 1024:
            return self.stripe(self.rfile.read(n))
        if n > 8192:  # a Google ID token is ~1.2 KB
            return self.send(413, page(note("Too much data.", True)))
        form = {k: v[0].strip() for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
        path = self.path.split("?")[0]
        if path == "/register":
            return self.register(form)
        m = re.fullmatch(r"/m/([\w-]{20,64})/(test|delete)", path)
        if not m or m[1] not in regs:
            return self.send(404, page(note("That link isn't active.", True)))
        token, action = m[1], m[2]
        if action == "test":
            r = regs[token]
            chat.post_discord(r["webhook"], f"LiveChat XR: test message ✔ Chat from {r['channel']} will show up here.")
            return self.manage(token, note("Test sent. Check your Discord channel (and your Quest)."))
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

    def stripe(self, body: bytes) -> None:
        ev = stripe_event(body, self.headers.get("Stripe-Signature", ""))
        if ev is None:
            return self.send(400, b"bad signature")
        obj = ev.get("data", {}).get("object", {})
        if ev.get("type") == "checkout.session.completed" and obj.get("payment_status") == "paid":
            token = obj.get("client_reference_id") or ""
            with lock:
                r = regs.get(token)
                if r and plan(r) != "free":
                    r.update(plan="paid", subscription=obj.get("subscription"))
                    save()
            if r and plan(r) == "paid":
                start(token)
                chat.post_discord(r["webhook"], "LiveChat XR: payment received ✔ It's on. Chat shows up here while you're live.")
        elif ev.get("type") == "customer.subscription.deleted":
            with lock:
                token = next((t for t, r in regs.items() if r.get("subscription") == obj.get("id")), None)
                if token:
                    regs[token]["plan"] = "pending"
                    save()
            if token:
                stop(token)
                chat.post_discord(regs[token]["webhook"], "LiveChat XR: your subscription ended, so chat posting is paused. "
                                                          f"Renew here: <{manage_url(token)}>")
        self.send(200, b"ok")

    def register(self, form: dict) -> None:
        wanted = {p: normalise_channel(p, form[p]) for p in LABEL if form.get(p, "").strip()}
        if not wanted and form.get("platform") in LABEL and form.get("channel", "").strip():  # older single-platform form
            wanted[form["platform"]] = normalise_channel(form["platform"], form["channel"])
        webhook = form.get("webhook", "")
        err = None
        if form.get("google") and GOOGLE_ID:
            try:
                form["name"], form["email"] = google_identity(form["google"])
                form["signin"] = "google"
            except Exception as e:
                log.warning("google sign-in: %r", e)
                err = "Google sign-in didn't go through. Try again, or type your name and email."
        name, email = form.get("name", "")[:60], form.get("email", "").lower()
        if err:
            pass
        elif not name or not EMAIL.match(email):
            err = "Please enter your name and a valid email."
        elif not wanted:
            err = "Enter your TikTok handle, your Twitch channel, your YouTube handle, or any mix."
        elif any(not CHANNEL[p].match(c) for p, c in wanted.items()):
            err = "That channel name doesn't look right."
        if err:
            return self.send(400, home(note(err, True), form))
        channel_note = ""
        for p, c in wanted.items():
            try:
                exists = check_channel(p, c)
            except Exception as e:
                log.warning("channel check: %s", type(e).__name__)  # no credentials or remote response in logs
                exists = None
            if exists is False:
                return self.send(400, home(note(f"We can't find that {LABEL[p]} account. Check the spelling, without the @", True), form))
            if exists is None:
                channel_note = "We couldn't verify your account right now. You're signed up; check the spelling if chat doesn't connect."
        if form.get("via") == "discord" and DISCORD_ID:
            state = secrets.token_urlsafe(24)
            with lock:
                oauth[state] = (time.time(), {k: form.get(k, "") for k in ("name", "email", "signin")} | wanted)
            q = urllib.parse.urlencode({"client_id": DISCORD_ID, "response_type": "code", "scope": "webhook.incoming",
                                        "redirect_uri": f"{PUBLIC_URL}/discord/callback", "state": state})
            return self.send(303, b"", location=f"https://discord.com/oauth2/authorize?{q}")
        elif not WEBHOOK.match(webhook):
            err = "That isn't a Discord webhook URL. It should start with https://discord.com/api/webhooks/"
        if err:
            return self.send(400, home(note(err, True), form))
        try:
            dname = check_webhook(webhook)
        except Exception:
            return self.send(400, home(note("Discord didn't accept that webhook. Copy it again from the channel's Integrations page.", True), form))
        with lock:
            token = next((t for t, r in regs.items() if r["webhook"] == webhook), None)
            if token is None and len(regs) >= MAX_REGS:
                return self.send(503, home(note("This server is full right now. Try again later.", True)))
            old = regs.get(token, {}) if token else {}
            token = token or secrets.token_urlsafe(24)
            p = plan(old) if old else ("free" if free_used() < FREE_SLOTS else "pending")
            platform, channel = next(iter(wanted.items()))  # primary pair kept for older readers (admin exports, QA tools)
            regs[token] = {**old, "name": name, "email": email, "platform": platform, "channel": channel, "channels": wanted, "webhook": webhook,
                           "discord_channel": dname, "created": old.get("created") or int(time.time()), "plan": p,
                           "signin": form.get("signin") or "email", "channel_note": channel_note}
            save()
        if p == "pending":
            chat.post_discord(webhook, f"LiveChat XR: almost done. Pick a plan to switch on chat from {channel_label(regs[token])}: <{manage_url(token)}>")
        else:
            start(token)
            chat.post_discord(webhook, f"LiveChat XR connected ✔ Chat from {channel_label(regs[token])} will show up here while you're live.\n"
                                       f"Manage or stop it: <{manage_url(token)}>")
        self.send(303, b"", location=f"/m/{token}?new={'google' if form.get('signin') == 'google' else 'email'}")

    def log_message(self, fmt, *args):  # keep tokens and query strings (admin key, OAuth codes) out of logs
        log.info("%s %s", self.command, re.sub(r"/m/[\w-]+", "/m/…", self.path.split("?")[0]))


def main() -> None:
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
    logging.getLogger("httpx").setLevel(logging.WARNING)  # TikTokLive logs every request URL
    try:
        regs.update(json.loads((DATA / "registrations.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    for i, token in enumerate(t for t, r in regs.items() if plan(r) != "pending"):
        start(token, delay=i * 3)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("listening on :%d with %d registrations", PORT, len(regs))
    loop.run_forever()


if __name__ == "__main__":
    main()

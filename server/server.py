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
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{15,22}/[\w-]{40,100}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
CHANNEL = {"tiktok": re.compile(r"^@?[A-Za-z0-9_.]{2,24}$"), "twitch": re.compile(r"^#?[A-Za-z0-9_]{3,25}$")}
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


async def run(token: str, delay: float = 0) -> None:
    await asyncio.sleep(delay)  # stagger startup: TikTok's sign server rate-limits connects per IP
    r = regs[token]

    def post(text: str) -> None:
        ok = chat.post_discord(r["webhook"], text) is not False
        st = stats.setdefault(token, {"posts": 0, "fails": 0, "last_post": 0, "last_error": ""})
        st["posts" if ok else "fails"] += 1
        if ok:
            st["last_post"] = time.time()
        else:
            st["last_error"] = time.strftime("%m-%d %H:%M") + " Discord post failed"

    def on_batch(text: str) -> None:
        threading.Thread(target=post, args=(text,), daemon=True).start()

    def on_status(s: str) -> None:
        if status.get(token) != s:
            log.info("%s: %s", r["channel"], s)
        status[token] = s

    while True:
        try:
            await chat.relay(r["platform"], r["channel"], on_batch, on_status)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            on_status(f"error, retrying ({type(e).__name__})")
            log.warning("%s: %r", r["channel"], e)
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
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LiveChat XR for Discord</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;background:#16131f;color:#eee}}
h1{{font-size:24px}}label{{display:block;margin:14px 0 4px}}input,select{{width:100%;padding:9px;font:inherit;border-radius:6px;
border:1px solid #555;background:#221d30;color:#eee;box-sizing:border-box}}button{{margin-top:16px;padding:10px 18px;font:inherit;
border:0;border-radius:6px;background:#7c4dff;color:#fff;cursor:pointer}}button.alt{{background:#444}}a{{color:#b39dff}}
.box{{background:#221d30;border-radius:8px;padding:14px 16px;margin:16px 0}}.err{{background:#5a1f2a}}small{{color:#aaa}}
form.inline{{display:inline}}</style></head><body>{body}</body></html>"""

HOME = """<h1>LiveChat XR for Discord</h1>
<p>Your TikTok LIVE or Twitch chat, posted to a Discord channel while you stream. With the Discord app on your
Quest, comments pop up in-game, no PC needed.</p>
{msg}
<form method="post" action="/register">
<label>Your name</label><input name="name" required maxlength="60">
<label>Email</label><input name="email" type="email" required maxlength="200">
<label>Platform</label><select name="platform"><option value="tiktok">TikTok LIVE</option><option value="twitch">Twitch</option></select>
<label>Channel / username</label><input name="channel" placeholder="@yourhandle" required maxlength="30">
{discord}</form>
<div class="box">{manual_help}
<li>In Discord, open a server you own (a new private one is fine) and make a channel like <code>#stream-chat</code>.</li>
<li>Channel settings (gear) → <b>Integrations</b> → <b>Webhooks</b> → <b>New Webhook</b> → <b>Copy Webhook URL</b>.</li>
<li>Paste it above and press Connect. You'll get a test message in that channel.</li></ol></details>
<b>On your Quest:</b> install Discord, sign in, open that channel → notification settings → <b>All Messages</b>.
Mute your other servers while streaming if you only want chat pop-ups.</div>
<p><small>The first few spots are free, then $3/month. Your name and email are only used to know who is using it.
Your webhook URL is only used to post your chat. Mentions are disabled, so chat can't ping anyone.
Open source: <a href="https://github.com/DeliciousHouse/livechat-xr">github.com/DeliciousHouse/livechat-xr</a></small></p>"""

MANAGE = """<h1>LiveChat XR for Discord</h1>{msg}
<div class="box"><b>{platform}:</b> {channel}<br><b>Discord:</b> webhook “{dname}”<br><b>Status:</b> {status}</div>{billing}
<p>Keep this page's link; it's also posted in your Discord channel. Leave it running: it picks up your chat
whenever you go live.</p>
<form class="inline" method="post" action="/m/{token}/test"><button>Send test message</button></form>
<form class="inline" method="post" action="/m/{token}/delete" onsubmit="return confirm('Stop posting chat to Discord?')">
<button class="alt">Stop and delete</button></form>
<p><a href="/">Set up another channel</a></p>"""


WEBHOOK_FIELD = ('<label>Discord webhook URL</label><input name="webhook" placeholder="https://discord.com/api/webhooks/…" '
                 'maxlength="200"{req}>')


def home(msg: str = "") -> bytes:
    if DISCORD_ID:
        discord = ('<button name="via" value="discord">Connect Discord</button><p><small>Discord asks which server and channel '
                   'to post in. Pick a channel in a server you own, e.g. a new <code>#stream-chat</code>.</small></p>'
                   '<details><summary><small>Or paste a webhook URL instead</small></summary>'
                   + WEBHOOK_FIELD.format(req="") + '<button name="via" value="webhook">Connect with webhook</button></details>')
        manual = '<details><summary><b>Getting a webhook URL by hand</b></summary><ol>'
    else:
        discord = WEBHOOK_FIELD.format(req=" required") + '<button>Connect</button>'
        manual = '<details open><summary><b>Getting a webhook URL (1 minute)</b></summary><ol>'
    return page(HOME.format(msg=msg, discord=discord, manual_help=manual))


def discord_exchange(code: str) -> str:
    """OAuth code -> the webhook URL Discord created in the channel the user picked."""
    body = urllib.parse.urlencode({"client_id": DISCORD_ID, "client_secret": DISCORD_SECRET, "grant_type": "authorization_code",
                                   "code": code, "redirect_uri": f"{PUBLIC_URL}/discord/callback"}).encode()
    req = urllib.request.Request("https://discord.com/api/oauth2/token", body,
                                 {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["webhook"]["url"]


def page(body: str) -> bytes:
    return PAGE.format(body=body).encode()


def note(text: str, err: bool = False) -> str:
    return f'<div class="box{" err" if err else ""}">{html.escape(text)}</div>'


def manage_url(token: str) -> str:
    return f"{PUBLIC_URL}/m/{token}" if PUBLIC_URL else f"/m/{token}"


def ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


def admin_body() -> str:
    e = html.escape
    live = sum("connected" in status.get(t, "") for t in regs)
    paid = sum(plan(r) == "paid" for r in regs.values())
    rows = []
    for t, r in sorted(regs.items(), key=lambda kv: -kv[1]["created"]):
        st = stats.get(t, {})
        rows.append(f"<tr><td>{e(r.get('name', ''))}<br><small>{e(r.get('email', ''))}</small></td>"
                    f"<td>{e(r['platform'])} {e(r['channel'])}<br><small>Discord: {e(r['discord_channel'])}</small></td>"
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
    return ('<meta http-equiv="refresh" content="30"><style>body{max-width:1100px}td{vertical-align:top;border-top:1px solid #333}'
            'th{text-align:left}</style><h1>LiveChat XR admin</h1>'
            f'<div class="box">{len(regs)} sign-ups: {free_used()}/{FREE_SLOTS} free, {paid} paid, '
            f'{sum(plan(r) == "pending" for r in regs.values())} waiting for payment. {live} live right now. '
            f'{warn} warnings in the recent log. <small>Counts since the last server restart; refreshes every 30 s.</small></div>'
            '<table cellpadding=6 width=100%><tr><th>User</th><th>Channel</th><th>Plan</th><th>Status</th><th>Posts</th>'
            f'<th>Failed posts</th><th>Joined</th></tr>{"".join(rows) or "<tr><td colspan=7>No sign-ups yet.</td></tr>"}</table>'
            f'<h2>Recent log</h2><table cellpadding=4 width=100%>{logs or "<tr><td>Empty.</td></tr>"}</table>')


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveChatXR"

    def send(self, code: int, body: bytes, location: str = "") -> None:
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")  # manage links carry the token
        self.end_headers()
        self.wfile.write(body)

    def manage(self, token: str, msg: str = "") -> None:
        r = regs.get(token)
        if not r:
            return self.send(404, page(note("That link isn't active. It may have been deleted.", True) + '<p><a href="/">Start over</a></p>'))
        billing, st = "", status.get(token, "starting…")
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
            msg=msg, token=token, platform="TikTok" if r["platform"] == "tiktok" else "Twitch",
            channel=html.escape(r["channel"]), dname=html.escape(r["discord_channel"]),
            status=html.escape(st), billing=billing)))

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
        if path == "/health":
            return self.send(200, f"ok {len(regs)}".encode())
        if m := re.fullmatch(r"/m/([\w-]{20,64})", path):
            return self.manage(m[1])
        self.send(404, page(note("Not found.", True)))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if self.path == "/stripe-webhook" and n <= 512 * 1024:
            return self.stripe(self.rfile.read(n))
        if n > 4096:
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
        platform = form.get("platform", "")
        channel = form.get("channel", "")
        webhook = form.get("webhook", "")
        name, email = form.get("name", "")[:60], form.get("email", "").lower()
        err = None
        if not name or not EMAIL.match(email):
            err = "Please enter your name and a valid email."
        elif platform not in CHANNEL or not CHANNEL[platform].match(channel):
            err = "That channel name doesn't look right."
        elif form.get("via") == "discord" and DISCORD_ID:
            state = secrets.token_urlsafe(24)
            with lock:
                oauth[state] = (time.time(), {k: form.get(k, "") for k in ("name", "email", "platform", "channel")})
            q = urllib.parse.urlencode({"client_id": DISCORD_ID, "response_type": "code", "scope": "webhook.incoming",
                                        "redirect_uri": f"{PUBLIC_URL}/discord/callback", "state": state})
            return self.send(303, b"", location=f"https://discord.com/oauth2/authorize?{q}")
        elif not WEBHOOK.match(webhook):
            err = "That isn't a Discord webhook URL. It should start with https://discord.com/api/webhooks/"
        if err:
            return self.send(400, home(note(err, True)))
        channel = ("@" + channel.lstrip("@")) if platform == "tiktok" else channel.lstrip("#").lower()
        try:
            dname = check_webhook(webhook)
        except Exception:
            return self.send(400, home(note("Discord didn't accept that webhook. Copy it again from the channel's Integrations page.", True)))
        with lock:
            token = next((t for t, r in regs.items() if r["webhook"] == webhook), None)
            if token is None and len(regs) >= MAX_REGS:
                return self.send(503, home(note("This server is full right now. Try again later.", True)))
            old = regs.get(token, {}) if token else {}
            token = token or secrets.token_urlsafe(24)
            p = plan(old) if old else ("free" if free_used() < FREE_SLOTS else "pending")
            regs[token] = {**old, "name": name, "email": email, "platform": platform, "channel": channel, "webhook": webhook,
                           "discord_channel": dname, "created": old.get("created") or int(time.time()), "plan": p}
            save()
        if p == "pending":
            chat.post_discord(webhook, f"LiveChat XR: almost done. Pick a plan to switch on chat from {channel}: <{manage_url(token)}>")
        else:
            start(token)
            chat.post_discord(webhook, f"LiveChat XR connected ✔ Chat from {channel} will show up here while you're live.\n"
                                       f"Manage or stop it: <{manage_url(token)}>")
        self.send(303, b"", location=f"/m/{token}")

    def log_message(self, fmt, *args):  # keep tokens out of logs
        log.info("%s %s", self.command, re.sub(r"/m/[\w-]+", "/m/…", self.path))


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

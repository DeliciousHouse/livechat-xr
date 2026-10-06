"""LiveChat XR relay server: people enter their TikTok/Twitch channel and a Discord webhook; the server
posts their stream chat to that Discord channel. The Discord app on a standalone Quest pops it up in-game.

No accounts: the webhook URL is the credential (whoever has it can already post there). Registering the
same webhook again updates it. Each registration gets a private manage link, which is also posted into
their Discord channel so it can't get lost.

    DATA_DIR=./data PORT=13300 python server.py      (needs app/chat.py next to it or on PYTHONPATH)
"""
import asyncio
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import chat

DATA = Path(os.environ.get("DATA_DIR", "data"))
PORT = int(os.environ.get("PORT", "13300"))
MAX_REGS = int(os.environ.get("MAX_REGS", "50"))  # ponytail: global cap, no per-IP limit; add one if strangers abuse it
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{15,22}/[\w-]{40,100}$")
CHANNEL = {"tiktok": re.compile(r"^@?[A-Za-z0-9_.]{2,24}$"), "twitch": re.compile(r"^#?[A-Za-z0-9_]{3,25}$")}
log = logging.getLogger("relay")

regs: dict[str, dict] = {}      # token -> {platform, channel, webhook, discord_channel, created}
status: dict[str, str] = {}     # token -> latest chat status line
tasks: dict[str, asyncio.Task] = {}
lock = threading.Lock()
loop = asyncio.new_event_loop()


def save() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / "registrations.tmp"
    tmp.write_text(json.dumps(regs, indent=1), encoding="utf-8")
    os.replace(tmp, DATA / "registrations.json")


def check_webhook(url: str) -> str:
    """Returns the webhook's display name; raises if Discord doesn't know it. Only Discord URLs get here."""
    req = urllib.request.Request(url, headers={"User-Agent": "LiveChatXR relay"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("name") or "webhook"


async def run(token: str, delay: float = 0) -> None:
    await asyncio.sleep(delay)  # stagger startup: TikTok's sign server rate-limits connects per IP
    r = regs[token]

    def on_batch(text: str) -> None:
        threading.Thread(target=chat.post_discord, args=(r["webhook"], text), daemon=True).start()

    def on_status(s: str) -> None:
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
<label>Platform</label><select name="platform"><option value="tiktok">TikTok LIVE</option><option value="twitch">Twitch</option></select>
<label>Channel / username</label><input name="channel" placeholder="@yourhandle" required maxlength="30">
<label>Discord webhook URL</label><input name="webhook" placeholder="https://discord.com/api/webhooks/…" required maxlength="200">
<button>Connect</button></form>
<div class="box"><b>Getting a webhook URL (1 minute)</b><ol>
<li>In Discord, open a server you own (a new private one is fine) and make a channel like <code>#stream-chat</code>.</li>
<li>Channel settings (gear) → <b>Integrations</b> → <b>Webhooks</b> → <b>New Webhook</b> → <b>Copy Webhook URL</b>.</li>
<li>Paste it above and press Connect. You'll get a test message in that channel.</li></ol>
<b>On your Quest:</b> install Discord, sign in, open that channel → notification settings → <b>All Messages</b>.
Mute your other servers while streaming if you only want chat pop-ups.</div>
<p><small>Your webhook URL is only used to post your chat. Mentions are disabled, so chat can't ping anyone.
Open source: <a href="https://github.com/DeliciousHouse/livechat-xr">github.com/DeliciousHouse/livechat-xr</a></small></p>"""

MANAGE = """<h1>LiveChat XR for Discord</h1>{msg}
<div class="box"><b>{platform}:</b> {channel}<br><b>Discord:</b> webhook “{dname}”<br><b>Status:</b> {status}</div>
<p>Keep this page's link; it's also posted in your Discord channel. Leave it running: it picks up your chat
whenever you go live.</p>
<form class="inline" method="post" action="/m/{token}/test"><button>Send test message</button></form>
<form class="inline" method="post" action="/m/{token}/delete" onsubmit="return confirm('Stop posting chat to Discord?')">
<button class="alt">Stop and delete</button></form>
<p><a href="/">Set up another channel</a></p>"""


def page(body: str) -> bytes:
    return PAGE.format(body=body).encode()


def note(text: str, err: bool = False) -> str:
    return f'<div class="box{" err" if err else ""}">{html.escape(text)}</div>'


def manage_url(token: str) -> str:
    return f"{PUBLIC_URL}/m/{token}" if PUBLIC_URL else f"/m/{token}"


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
        self.send(200, page(MANAGE.format(
            msg=msg, token=token, platform="TikTok" if r["platform"] == "tiktok" else "Twitch",
            channel=html.escape(r["channel"]), dname=html.escape(r["discord_channel"]),
            status=html.escape(status.get(token, "starting…")))))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(200, page(HOME.format(msg="")))
        if path == "/health":
            return self.send(200, f"ok {len(regs)}".encode())
        if m := re.fullmatch(r"/m/([\w-]{20,64})", path):
            return self.manage(m[1])
        self.send(404, page(note("Not found.", True)))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
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
            regs.pop(token, None)
            save()
        stop(token)
        self.send(200, page(note("Stopped and deleted. Nothing more will be posted.") + '<p><a href="/">Set up again</a></p>'))

    def register(self, form: dict) -> None:
        platform = form.get("platform", "")
        channel = form.get("channel", "")
        webhook = form.get("webhook", "")
        err = None
        if platform not in CHANNEL or not CHANNEL[platform].match(channel):
            err = "That channel name doesn't look right."
        elif not WEBHOOK.match(webhook):
            err = "That isn't a Discord webhook URL. It should start with https://discord.com/api/webhooks/"
        if err:
            return self.send(400, page(HOME.format(msg=note(err, True))))
        channel = ("@" + channel.lstrip("@")) if platform == "tiktok" else channel.lstrip("#").lower()
        try:
            dname = check_webhook(webhook)
        except Exception:
            return self.send(400, page(HOME.format(msg=note("Discord didn't accept that webhook. Copy it again from the channel's Integrations page.", True))))
        with lock:
            token = next((t for t, r in regs.items() if r["webhook"] == webhook), None)
            if token is None and len(regs) >= MAX_REGS:
                return self.send(503, page(HOME.format(msg=note("This server is full right now. Try again later.", True))))
            token = token or secrets.token_urlsafe(24)
            regs[token] = {"platform": platform, "channel": channel, "webhook": webhook,
                           "discord_channel": dname, "created": regs.get(token, {}).get("created") or int(time.time())}
            save()
        start(token)
        chat.post_discord(webhook, f"LiveChat XR connected ✔ Chat from {channel} will show up here while you're live.\n"
                                   f"Manage or stop it: <{manage_url(token)}>")
        self.send(303, b"", location=f"/m/{token}")

    def log_message(self, fmt, *args):  # keep tokens out of logs
        log.info("%s %s", self.command, re.sub(r"/m/[\w-]+", "/m/…", self.path))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # TikTokLive logs every request URL
    try:
        regs.update(json.loads((DATA / "registrations.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    for i, token in enumerate(regs):
        start(token, delay=i * 3)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("listening on :%d with %d registrations", PORT, len(regs))
    loop.run_forever()


if __name__ == "__main__":
    main()

"""Loopback-only real relay UI with disposable data and simulated transport."""
import argparse
import asyncio
import contextlib
import re
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "server")]
import chat
import server

# These are deliberately inert, internally constructed values, never operator credentials.
HOOKS = {f"fixture-{name}": "https://discord.com/api/webhooks/" + str(1000000000000000000 + i) + "/" + "x" * 68
         for i, name in enumerate(("one", "two"))}
LABEL = "Isolated simulated transport — not actual Discord, Google or Stripe proof."


def check_channel(platform, channel):
    handle = channel.lstrip("@#").lower()
    if handle == "qa_nonexistent":
        return False
    if handle == "qa_lookup_error":
        raise TimeoutError("simulated lookup error")
    return True  # qa_offline and other syntactically valid handles are simulated offline accounts


def check_webhook(url):
    if url not in HOOKS.values():
        raise ValueError("use fixture-one or fixture-two")
    return {"name": "QA simulated Discord"}


def start(token, delay=0):
    outcome = "lookup inconclusive" if server.regs[token]["channel"].lstrip("@#").lower() == "qa_lookup_error" else "account exists, offline"
    server.status[token] = f"Simulated: {outcome}; no chat connection"


def reset():
    with server.lock:
        server.regs.clear()
        server.founders.clear()
        server.status.clear()
        server.stats.clear()
        server.oauth.clear()
        server.save()


class Handler(server.Handler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'")
        super().end_headers()

    def send(self, code, body, location="", ctype="text/html; charset=utf-8"):
        if not ctype.startswith("text/html"):
            return super().send(code, body, location, ctype)
        if b"<body>" in body:
            body = body.replace(b"<body>", ("<body>" + server.note(LABEL)).encode())
        for handle, hook in HOOKS.items():
            body = body.replace(hook.encode(), handle.encode())
        body = body.replace(b"Test sent. Check your Discord channel (and your Quest).",
                            b"Simulated test accepted. No Discord message was sent.")
        body = body.replace(b"Keep this page's link; it's also posted in your Discord channel. Leave it running: it picks up your chat\nwhenever you go live.",
                            b"Keep this fixture manage link for test/delete. No real chat is connected or posted.")
        super().send(code, body, location, ctype)

    def register(self, form, edit_token=""):
        form["webhook"] = HOOKS.get(form.get("webhook"), form.get("webhook", ""))
        super().register(form, edit_token)

    def do_GET(self):
        if self.path.split("?")[0].startswith("/qa-payment/"):
            return self.send(200, server.page(server.note("Inert payment fixture. No payment is collected.")))
        super().do_GET()

    def do_POST(self):
        if self.path == "/__qa/reset":
            reset()
            return self.send(200, b"reset: zero registrations")
        if self.path == "/__qa/stop":
            self.send(200, b"stopping isolated fixture")
            self.server.stopped.set()
            return
        super().do_POST()

    def log_message(self, *args):
        pass  # no tokens, webhook values, query strings or submitted form data in launcher logs


NETWORK_LABEL = "SYNTHETIC VIEW-ONLY — no providers, delivery, onboarding or Quest proof."
NETWORK_TOKEN = "synthetic-registration-0001"
NETWORK_ADMIN = "synthetic-view-only"
SCENARIOS = ("normal", "delayed", "closed-dm")


class NetworkHandler(Handler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
        server.Handler.end_headers(self)

    def send(self, code, body, location="", ctype="text/html; charset=utf-8"):
        if location:
            code, body = 403, server.page(server.note("Fixture redirects disabled."))
        if ctype.startswith("text/html"):
            # Only fixed repository templates and synthetic fields reach this boundary.
            body = re.sub(rb'<script\b[^>]*>.*?</script>', b'', body, flags=re.S | re.I)
            body = re.sub(rb'<link\b[^>]*>|<meta\b[^>]*http-equiv=[^>]*>', b'', body, flags=re.I)
            body = re.sub(rb'<form\b[^>]*>', b'<fieldset disabled style="border:0;padding:0;margin:0">', body, flags=re.I)
            body = re.sub(rb'</form>', b'</fieldset>', body, flags=re.I)
            allowed = {b'/', b'/privacy', b'/terms', ("/m/" + NETWORK_TOKEN).encode(),
                       ("/admin?key=" + NETWORK_ADMIN).encode()}
            body = re.sub(rb'href="([^"]*)"', lambda m: m[0] if m[1] in allowed else b'aria-disabled="true"', body)
            body = body.replace(b'Starting: checking your channel automatically.', b'Synthetic channel; no lookup or collector running.')
            body = body.replace(b'Waiting for chat messages.', b'No chat transport in this fixture.')
            body = body.replace(b'Chat relay: running', b'Fixture event loop: running; no chat collectors')
            body = re.sub(rb'Discovery network off .*?restart the relay\.', b'Discovery intentionally off; empty synthetic config.', body)
            body = body.replace(b'Status: Listed when', b'Synthetic status: Listed when')
            body = body.replace('Channel ownership verified ✓'.encode(), b'Synthetic channel proof state')
            body = body.replace(b'Pop1 Live: you&#x27;re on.', b'Synthetic reviewed-listing copy: you&#x27;re on.')
            body = body.replace(b'Refreshes every 30 s.', b'Static startup scenario; no automatic refresh.')
            body = body.replace(b'</head>', b'<style>fieldset:disabled button{background:var(--line);color:var(--muted);'
                                b'box-shadow:none;cursor:not-allowed}fieldset:disabled input,fieldset:disabled select'
                                b'{color:var(--muted);cursor:not-allowed}</style></head>')
            body = body.replace(b'<body>', ('<body>' + server.note(NETWORK_LABEL + " Scenario: " + self.server.scenario)).encode())
        server.Handler.send(self, code, body, ctype=ctype)

    def send_error(self, code, message=None, explain=None):
        self.send(code, server.page(server.note("Invalid fixture request.", True)))

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.command != "GET":
            self.close_connection = True
            self.send(405, server.page(server.note("Synthetic fixture is GET-only.")))
            return False
        return True

    def do_GET(self):
        if self.path not in ("/", "/privacy", "/terms", "/pretext.js", "/m/" + NETWORK_TOKEN,
                             "/admin?key=" + NETWORK_ADMIN):
            return self.send(404, server.page(server.note("Outside view-only fixture scope.")))
        server.Handler.do_GET(self)


@contextlib.contextmanager
def network_state(data, scenario):
    net = server.network.Network(data, server.normalise_channel, {})
    net.store.initialize()  # Deliberately never Network.start: no collectors, commands or maintenance.
    now = time.time()
    with net.store.connect() as db:
        lid = db.execute("INSERT INTO listings(discord_id,opted_in,review_status,dm_closed,ign,timezone) "
                         "VALUES ('1',1,'reviewed',?,'Synthetic streamer','America/Los_Angeles')",
                         (scenario == "closed-dm",)).lastrowid
        db.execute("INSERT INTO channels(listing_id,platform,platform_id,handle,observed_at,verified) "
                   "VALUES (?,'twitch','1','synthetic_streamer',?,1)", (lid, now))
    for source in net.sources.values():
        source.update(last_success=now, success_streak=2)
    if scenario == "delayed":
        net.sources["twitch"].update(delayed=True, incident_since=now - 1200, error="synthetic delay")
    net._discord = lambda method, path, payload: (200, {"id": "synthetic-only"})
    loop = asyncio.new_event_loop()
    net.loop = loop
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        with mock.patch.object(server, "loop", loop), mock.patch.object(server, "discovery", net):
            server.regs[NETWORK_TOKEN] = dict(name="Synthetic streamer", email="synthetic@example.invalid", webhook="",
                discord_channel="Synthetic channel", platform="twitch", channel="synthetic_streamer", channel_id="1",
                guild_id="1", plan="founder", created=now, network_requested=True, network_listing=lid, network_owner="1")
            server.status[NETWORK_TOKEN] = "Synthetic offline relay; no chat connection"
            yield thread
    finally:
        try:
            asyncio.run_coroutine_threadsafe(net.stop(), loop).result(5)
            asyncio.run_coroutine_threadsafe(loop.shutdown_asyncgens(), loop).result(5)
            asyncio.run_coroutine_threadsafe(loop.shutdown_default_executor(), loop).result(5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()


@contextlib.contextmanager
def fixture(port=0, *, network_fixture=False, scenario="normal"):
    """Own only this temporary directory and listener; restore adapters on exit."""
    if scenario not in SCENARIOS or (not network_fixture and scenario != "normal"):
        raise ValueError("scenario requires network-fixture and an enumerated startup choice")
    with tempfile.TemporaryDirectory(prefix="livechatxr-qa-") as data, contextlib.ExitStack() as stack:
        values = dict(DATA=Path(data), FREE_SLOTS=1, MAX_REGS=2, PAY_MONTHLY="/qa-payment/monthly",
                      PAY_YEARLY="/qa-payment/yearly", BILLING_URL="/qa-payment/billing", PUBLIC_URL="",
                      STRIPE_SECRET="", DISCORD_ID="", DISCORD_SECRET="", GA_ID="", ADMIN_KEY="",
                      regs={}, founders=set(), status={}, stats={}, tasks={}, oauth={}, check_channel=check_channel,
                      check_webhook=check_webhook, start=start, stop=lambda token: server.status.pop(token, None))
        for key, value in values.items():
            stack.enter_context(mock.patch.object(server, key, value))
        if network_fixture:
            for key, value in dict(DONATE_URL="", PAY_MONTHLY="", PAY_YEARLY="", BILLING_URL="",
                                   ADMIN_KEY=NETWORK_ADMIN, NETWORK_WEB_PRIVATE=False, reports={}, logbuf=[],
                                   network_oauth={}, network_csrf={}).items():
                stack.enter_context(mock.patch.object(server, key, value))
            stack.enter_context(mock.patch("urllib.request.OpenerDirector.open", side_effect=RuntimeError("external network disabled")))
            loop_thread = stack.enter_context(network_state(data, scenario))
        stack.enter_context(mock.patch.object(chat, "post_discord", lambda url, text, **kwargs: url in HOOKS.values()))
        stack.enter_context(mock.patch.object(chat, "resolve_identity", return_value=None))
        # Fail closed even if an unexpected request reaches an otherwise unused OAuth boundary.
        stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=RuntimeError("external network disabled")))
        home = server.home

        def fixture_home(msg="", form=None):
            return home(msg, form if form is not None else {"name": "QA Fixture", "email": "qa@example.invalid"}).replace(
                "https://discord.com/api/webhooks/…".encode(), b"fixture-one or fixture-two")

        stack.enter_context(mock.patch.object(server, "home", fixture_home))
        srv = ThreadingHTTPServer(("127.0.0.1", port), NetworkHandler if network_fixture else Handler)
        srv.stopped = threading.Event()
        srv.scenario = scenario
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        srv.fixture_http_thread = thread
        if network_fixture:
            srv.fixture_loop_thread = loop_thread
        thread.start()
        try:
            yield srv
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()
            if not network_fixture:
                reset()


def main():
    parser = argparse.ArgumentParser(description=LABEL)
    parser.add_argument("--port", type=int, default=13301, help="loopback port (0 chooses an unused port)")
    parser.add_argument("--network-fixture", action="store_true", help="synthetic view-only listing/manage/admin; no onboarding")
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal", help="network-fixture startup state only")
    args = parser.parse_args()
    if not args.network_fixture and args.scenario != "normal":
        parser.error("--scenario requires --network-fixture")
    with fixture(args.port, network_fixture=args.network_fixture, scenario=args.scenario) as srv:
        print(f"{NETWORK_LABEL if args.network_fixture else LABEL}\nhttp://127.0.0.1:{srv.server_port}/", flush=True)
        if args.network_fixture:
            print(f"Synthetic reads: /m/{NETWORK_TOKEN} and /admin?key={NETWORK_ADMIN}; Ctrl+C cleans up. No reset/stop routes.", flush=True)
        else:
            print("Handles: fixture-one, fixture-two; Ctrl+C or POST /__qa/stop removes fixture data.", flush=True)
        try:
            srv.stopped.wait()
        except KeyboardInterrupt:
            pass
    print("Stopped: disposable data removed; no fixture registrations remain.", flush=True)


if __name__ == "__main__":
    main()

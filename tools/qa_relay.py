"""Loopback-only real relay UI with disposable data and simulated transport (stdlib only)."""
import argparse
import contextlib
import sys
import tempfile
import threading
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
    return "QA simulated Discord"


def start(token, delay=0):
    outcome = "lookup inconclusive" if server.regs[token]["channel"].lstrip("@#").lower() == "qa_lookup_error" else "account exists, offline"
    server.status[token] = f"Simulated: {outcome}; no chat connection"


def reset():
    with server.lock:
        server.regs.clear()
        server.status.clear()
        server.stats.clear()
        server.oauth.clear()
        server.save()


class Handler(server.Handler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'")
        super().end_headers()

    def send(self, code, body, location=""):
        if b"<body>" in body:
            body = body.replace(b"<body>", ("<body>" + server.note(LABEL)).encode())
        for handle, hook in HOOKS.items():
            body = body.replace(hook.encode(), handle.encode())
        body = body.replace(b"Test sent. Check your Discord channel (and your Quest).",
                            b"Simulated test accepted. No Discord message was sent.")
        body = body.replace(b"Keep this page's link; it's also posted in your Discord channel. Leave it running: it picks up your chat\nwhenever you go live.",
                            b"Keep this fixture manage link for test/delete. No real chat is connected or posted.")
        super().send(code, body, location)

    def register(self, form):
        form["webhook"] = HOOKS.get(form.get("webhook"), form.get("webhook", ""))
        super().register(form)

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


@contextlib.contextmanager
def fixture(port=0):
    """Own only this temporary directory and listener; restore adapters on exit."""
    with tempfile.TemporaryDirectory(prefix="livechatxr-qa-") as data, contextlib.ExitStack() as stack:
        values = dict(DATA=Path(data), FREE_SLOTS=1, MAX_REGS=2, PAY_MONTHLY="/qa-payment/monthly",
                      PAY_YEARLY="/qa-payment/yearly", BILLING_URL="/qa-payment/billing", PUBLIC_URL="",
                      STRIPE_SECRET="", DISCORD_ID="", DISCORD_SECRET="", GOOGLE_ID="", GA_ID="", ADMIN_KEY="",
                      regs={}, status={}, stats={}, tasks={}, oauth={}, check_channel=check_channel,
                      check_webhook=check_webhook, start=start, stop=lambda token: server.status.pop(token, None))
        for key, value in values.items():
            stack.enter_context(mock.patch.object(server, key, value))
        stack.enter_context(mock.patch.object(chat, "post_discord", lambda url, text: url in HOOKS.values()))
        # Fail closed even if an unexpected request reaches an otherwise unused OAuth boundary.
        stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=RuntimeError("external network disabled")))
        home = server.home

        def fixture_home(msg="", form=None):
            return home(msg, form if form is not None else {"name": "QA Fixture", "email": "qa@example.invalid"}).replace(
                "https://discord.com/api/webhooks/…".encode(), b"fixture-one or fixture-two")

        stack.enter_context(mock.patch.object(server, "home", fixture_home))
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        srv.stopped = threading.Event()
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield srv
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()
            reset()


def main():
    parser = argparse.ArgumentParser(description=LABEL)
    parser.add_argument("--port", type=int, default=13301, help="loopback port (0 chooses an unused port)")
    args = parser.parse_args()
    with fixture(args.port) as srv:
        print(f"{LABEL}\nhttp://127.0.0.1:{srv.server_port}/", flush=True)
        print("Handles: fixture-one, fixture-two; Ctrl+C or POST /__qa/stop removes fixture data.", flush=True)
        try:
            srv.stopped.wait()
        except KeyboardInterrupt:
            pass
    print("Stopped: disposable data removed; no fixture registrations remain.", flush=True)


if __name__ == "__main__":
    main()

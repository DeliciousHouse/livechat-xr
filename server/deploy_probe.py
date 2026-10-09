"""Run via docker exec -i ... python -; never print admin keys or manage URLs.

Uses the existing manage page so the first deployment can probe older images.
Only hashed registration IDs and connection booleans leave the container.
"""
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


class Status(HTMLParser):
    def __init__(self):
        super().__init__()
        self.label = False
        self.value = ""

    def handle_data(self, data):
        if self.label:
            self.value = data.strip()
            self.label = False
        elif data.strip() == "Status:":
            self.label = True


def snapshot():
    base = "http://127.0.0.1:" + os.environ.get("PORT", "13300")
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("home unhealthy")
    try:
        urllib.request.urlopen(base + "/admin", timeout=10).close()
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403, 404):
            raise RuntimeError("admin unhealthy") from None
    else:
        raise RuntimeError("admin is not protected")
    regs = json.loads((Path(os.environ.get("DATA_DIR", "/data")) / "registrations.json").read_text())
    connected = []
    for token in regs:
        parser = Status()
        # Tokens originate from the local registry, not remote input.
        if not token or not all(c.isalnum() or c in "-_" for c in token):
            raise RuntimeError("invalid registration ID")
        with urllib.request.urlopen(base + "/m/" + token, timeout=10) as response:
            parser.feed(response.read(1_000_000).decode("utf-8"))
        if not parser.value:
            raise RuntimeError("manage page has no status")
        if ": connected to " in parser.value:
            connected.append(hashlib.sha256(token.encode()).hexdigest())
    return {"registrations": sorted(hashlib.sha256(t.encode()).hexdigest() for t in regs),
            "connected": sorted(connected)}


if __name__ == "__main__":
    try:
        print(json.dumps(snapshot()))
    except Exception:
        # HTTPError includes secret-bearing manage URLs. Keep exceptions private.
        raise SystemExit("relay probe failed") from None

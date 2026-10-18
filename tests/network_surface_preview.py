"""Render private payload previews, NOT Discord/Quest delivery or activation evidence.

Run from repository root: python -m tests.network_surface_preview <output-dir>
Only fake API/Discord transports. No interaction identifiers/tokens are exported.
"""
import asyncio
import html
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from tests.test_network import config, network, row, server


def markup(text):
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[(Watch on (?:Twitch|YouTube))\]\((https://[^\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


async def render(output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        n = network.Network(directory, server.normalise_channel, config())
        n.store.initialize()
        n.enabled = True
        now = time.time()
        entries = []
        for i, (name, count) in enumerate((("Naiya", 1), ("Pop", 3), ("Brendan", 4))):
            lid = n.store.owner_listing(str(i + 7), create=True)["id"]
            entries.append(dict(row(f"fixture{i}"), platform_id=str(i + 42), viewers=count))
            n.store.upsert_presence("twitch", entries, now)
            n.store.record_supplied_channel(lid, "twitch", f"fixture{i}")
            n.store.bind_verified_channel(lid, "twitch", f"fixture{i}", str(i + 42))
            n.store.set_listing(lid, opted_in=True, review_status="reviewed")
            n.store.update_profile(lid, ign=name)
        for platform in network.INTERVALS: n.record_poll(platform, now=now)
        live = await n.render_live(now)
        n.resolve_identity = mock.AsyncMock(return_value={"id": "99", "handle": "fixture"})
        proof = await n.command(dict(type=2, id="fixture", token="fixture", guild_id="123",
            member={"user": {"id": "9"}}, data={"name": "optin", "options": [
                {"name": "platform", "value": "twitch"}, {"name": "handle", "value": "fixture"}]}))
        proof["content"] = re.sub(r"```\n.*?\n```", "[private verification code omitted]", proof["content"], flags=re.S)
        sections = [("/live", live["content"]), ("/optin", proof["content"]),
                    ("Review pending", network.PENDING), ("DMs blocked", network.BLOCKED_DM)]
        body = "".join(f'<section><h2>{html.escape(title)}</h2><div>{markup(text)}</div></section>' for title, text in sections)
        document = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pop1 Live private payload fixture</title><style>
*{box-sizing:border-box}body{margin:0;background:#0f0d16;color:#eceaf4;font:15px/1.55 system-ui,sans-serif}
main{max-width:680px;padding:24px 20px;margin:auto}h1{font-size:22px;line-height:1.3;margin:0 0 12px}h2{font-size:17px;margin:0 0 12px}
p{color:#c9c5d8;margin:0 0 24px}section{padding:20px 0;border-top:1px solid #332d42}section div{white-space:pre-wrap;overflow-wrap:anywhere}
a{color:#b39dff;text-decoration:underline;text-underline-offset:3px}a:focus-visible{outline:2px solid #8b5cf6;outline-offset:3px}
</style><main><h1>Pop1 Live · private fixture</h1><p>Synthetic streamers, real command/renderer output. Payload preview only: Discord fonts, timestamps, delivery and Quest settings are not reproduced.</p>'''
        (output / "index.html").write_text(document + body + "</main></html>", encoding="utf-8")
        await n.stop()


if __name__ == "__main__": asyncio.run(render(Path(sys.argv[1])))

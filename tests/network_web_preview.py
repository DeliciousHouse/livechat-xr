"""Sanitized PRIVATE fixture pages from the real local HTTP harness; never a live service.

Run: python -m tests.network_web_preview <output-dir>
No OAuth, Discord or platform network calls. Output strips private form targets and CSRF.
"""
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from tests.test_network_web import Web, server
from tests.network_surface_preview import render


def generate(output):
    output.mkdir(parents=True, exist_ok=True)
    fixture = Web()
    fixture.setUp()
    server.GA_ID = ""  # Preview does not contact analytics; the HTTP security tests exercise configured GA.
    checks = []
    try:
        def capture(name, result):
            code, headers, text = result
            assert code == 200
            for secret in (fixture.token, fixture.other, *server.network_csrf.values(), *server.network_oauth):
                text = text.replace(secret, "PRIVATE-FIXTURE-OMITTED")
            text = re.sub(r'(<input[^>]*name="csrf"[^>]*value=")[^"]*', r'\1OMITTED', text)
            text = text.replace('<main>', '<main><p role="note" style="color:var(--accent)">PRIVATE FIXTURE · synthetic data · not deployed · Quest unverified</p>')
            # Static copies cannot submit to a real service; preserve layout and normal field behavior.
            text = re.sub(r'<form\b', '<form onsubmit="return false"', text)
            text = re.sub(r'"/m/[^\"]*"', '"#private-fixture"', text)
            text = text.replace('href="/"', 'href="home.html"').replace('href="/privacy"', 'href="privacy.html"').replace('href="/terms"', 'href="terms.html"')
            for secret in (fixture.token, fixture.other, *server.network_csrf.values(), *server.network_oauth):
                assert secret not in text
            (output / (name + '.html')).write_text(text, encoding='utf-8')
            checks.append(dict(page=name, http_status=code, private_fields_removed=True))
        capture('home', fixture.request('/'))
        capture('privacy', fixture.request('/privacy'))
        capture('terms', fixture.request('/terms'))
        capture('manage-start', fixture.manage())
        fixture.connect()
        capture('manage-profile', fixture.manage())
        result = fixture.post('profile', ign='Fixture streamer', timezone='America/Los_Angeles')
        capture('manage-proof', result)
        fixture.net.enabled = False
        fixture.net.missing = ['DISCORD_BOT_TOKEN']
        fixture.net.sources['twitch'].update(delayed=True, incident_since=time.time()-1320, error='HTTP 429')
        fixture.net.sources['youtube'].update(delayed=True, incident_since=time.time()-1080, error='HTTP 403 quota')
        fixture.net.store.set_listing(fixture.reg['network_listing'], dm_closed=True)
        capture('admin-incidents', fixture.request('/admin?key=fixture'))
        (output / 'pretext.js').write_bytes(server.PRETEXT)
    finally:
        fixture.tearDown()
        fixture.doCleanups()
    asyncio.run(render(output / 'discord'))
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip())
    receipt = dict(source_sha=sha, dirty=dirty, kind='PRIVATE synthetic HTTP and fake Discord REST fixture',
                   http=checks, limitations=['No live OAuth registration', 'No Discord or Quest delivery',
                   'No deployed policies', 'No quota approval or terms clearance', 'E6 unavailable'])
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__': generate(Path(sys.argv[1]))

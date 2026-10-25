"""Synthetic, loopback-only visual receipt for the actual inspector, not live acceptance."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.test_inspection import Boundary
from playwright.sync_api import sync_playwright

SOURCE = 'b083eccae7aaa9b2aaa0017c47ccc99ff6a1d756'
OUT = Path(__file__).resolve().parent
fixture = Boundary()
fixture.setUp()
try:
    fixture.export.revision = fixture.viewer.revision = SOURCE
    fixture.net.store.update_profile(fixture.lid, ign='Synthetic creator')
    receipts = []
    blocked = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True,
            args=['--disable-background-networking', '--disable-component-update'])
        try:
            context = browser.new_context()
            def route(request):
                if urlsplit(request.request.url).hostname == '127.0.0.1': request.continue_()
                else:
                    blocked.append(request.request.url)
                    request.abort()
            context.route('**/*', route)
            for state in ('available', 'observed-empty', 'unavailable'):
                if state == 'observed-empty': fixture.net.store.set_listing(fixture.lid, opted_in=False)
                if state == 'unavailable': fixture.net.sources['youtube']['last_success'] = 0
                for name, width, height in (('desktop', 1440, 900), ('mobile', 390, 844)):
                    page = context.new_page()
                    page.set_viewport_size({'width': width, 'height': height})
                    response = page.goto(f'http://127.0.0.1:{fixture.http.server_port}/live')
                    page.screenshot(path=str(OUT / f'{state}-{name}.png'), full_page=False)
                    receipts.append(dict(state=state, viewport=[width, height], status=response.status,
                        overflow=page.evaluate('Math.max(0,document.documentElement.scrollWidth-innerWidth)'),
                        title=page.title(), screenshot=f'{state}-{name}.png'))
                    page.close()
            context.close()
        finally: browser.close()
    (OUT / 'visual-receipt.json').write_text(json.dumps(dict(synthetic=True, source_revision=SOURCE,
        source_digest=fixture.export.digest, offhost_page_requests=blocked, captures=receipts), indent=2) + '\n')
    print(json.dumps(receipts, indent=2))
finally:
    fixture.doCleanups()

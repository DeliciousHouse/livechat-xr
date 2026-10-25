"""Bounded sanitized cache inspection. No collectors, relay import or provider requests.

The runtime owns Projection; only its sanitized GET /projection crosses to Viewer.
A file replay is inspectable but can never authorize a Watch redirect.
"""
import argparse
import asyncio
import hashlib
import html
import http.client
import json
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import network

MAX_BYTES = 262144
MAX_AGE = 15
MAX_CHANNELS = 200
ELIGIBILITY = 'Store.discovery/live_channels: verified, opted-in, reviewed; Pop1 primary 0–10; observation <1800s'
TOKEN = r'[A-Za-z0-9_-]{22}'


def utc(stamp):
    # Floor, do not round a generation instant into the future by a microsecond.
    whole = math.floor(stamp)
    value = datetime.fromtimestamp(whole, timezone.utc).replace(microsecond=int((stamp - whole) * 1000000))
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def stamp(value):
    if not isinstance(value, str): raise ValueError('timestamp unavailable')
    parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    if parsed.isoformat(timespec='microseconds').replace('+00:00', 'Z') != value:
        raise ValueError('noncanonical timestamp')
    return parsed.timestamp()


def source_digest():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ('inspection.py', 'network.py', 'backup.py', 'chat.py', 'server.py'):
        path = root / name
        if name == 'chat.py' and not path.is_file(): path = root.parent / 'app' / name
        digest.update(name.encode() + b'\0' + path.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    return digest.hexdigest()


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('exact source revision required')
    return value


def encode(packet):
    body = json.dumps(packet, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(body) > MAX_BYTES: raise ValueError('projection too large')
    return body


class Projection:
    def __init__(self, runtime, source_revision):
        self.runtime = runtime
        self.revision = revision(source_revision)
        self.digest = source_digest()
        # Rendering is enabled in this cache-only object; start() is NEVER invoked.
        self.presenter = network.Network(runtime.store.path.parent, lambda p, c: c, config={'PUBLIC_URL': ''})
        self.presenter.store = runtime.store
        self.presenter.enabled = True
        self.guard = threading.Lock()

    def read(self):
        with self.guard, self.runtime.store.guard:
            now = time.time()
            polls = {}
            for p, state in self.runtime.snapshot()['sources'].items():
                last = state['last_success']
                polls[p] = utc(last) if type(last) in (int, float) and math.isfinite(last) and 0 < last <= now else None
            packet = dict(schema=1, source_revision=self.revision, source_digest=self.digest,
                generated_at=utc(now), eligibility=ELIGIBILITY, availability='unavailable',
                eligible_count=None, unknown_primary_count=None, polls=polls, rows=[], text='Cache unavailable. No observed population can be claimed.', watches={})
            try:
                public = self.runtime.store.inspection_rows(now)
                rows = public['rows']
                unknown = public['unknown_primary_count']
                if len(rows) > 100: raise ValueError('inspection capacity exceeded')
                # Empty requires actual complete successful polls, not process start or file mtime.
                if not rows and (unknown or any(v is None or now - stamp(v) >= 1800 for v in polls.values())):
                    return packet
                self.presenter.sources = {p: dict(s) for p, s in self.runtime.snapshot()['sources'].items()}
                rendered = asyncio.run(self.presenter.render_live(now, inspection=True))['content']
                watches = {}
                destinations = {c['destination'] for r in rows for c in r['channels']}
                for key in re.findall(r'/go/(' + TOKEN + r')', rendered):
                    destination = self.presenter.resolve_go(key, limit=201)
                    if destination in destinations: watches[key] = destination
                packet.update(availability='available' if rows else 'observed-empty', eligible_count=len(rows),
                              unknown_primary_count=unknown, rows=rows, text=rendered, watches=watches)
                validate(encode(packet), self.revision, self.digest, now)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
                packet.update(availability='unavailable', eligible_count=None, unknown_primary_count=None, rows=[], watches={},
                              text='Cache unavailable. No observed population can be claimed.')
            return packet


def validate(body, expected_revision, expected_digest, now=None):
    now = time.time() if now is None else now
    if len(body) > MAX_BYTES: raise ValueError('projection too large')
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result: raise ValueError('duplicate field')
            result[k] = v
        return result
    p = json.loads(body, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    fields = {'schema', 'source_revision', 'source_digest', 'generated_at', 'eligibility', 'availability',
              'eligible_count', 'unknown_primary_count', 'polls', 'rows', 'text', 'watches'}
    if not isinstance(p, dict) or set(p) != fields or type(p['schema']) is not int or p['schema'] != 1:
        raise ValueError('unsupported contract')
    if p['source_revision'] != expected_revision or p['source_digest'] != expected_digest or p['eligibility'] != ELIGIBILITY:
        raise ValueError('source binding mismatch')
    generated = stamp(p['generated_at'])
    if not 0 <= now - generated < MAX_AGE: raise ValueError('expired projection')
    if p['availability'] not in ('available', 'observed-empty', 'unavailable'): raise ValueError('availability')
    if not isinstance(p['polls'], dict) or set(p['polls']) != set(network.INTERVALS): raise ValueError('polls')
    for v in p['polls'].values():
        if v is not None and not 0 < stamp(v) <= generated: raise ValueError('poll timestamp')
    if not isinstance(p['text'], str) or len(p['text']) > 6000: raise ValueError('text bound')
    if not isinstance(p['rows'], list) or len(p['rows']) > 100: raise ValueError('row bound')
    count = p['eligible_count']
    unknown = p['unknown_primary_count']
    if unknown is not None and (type(unknown) is not int or not 0 <= unknown <= MAX_CHANNELS):
        raise ValueError('unknown primary bound')
    if p['availability'] == 'unavailable':
        if count is not None or unknown is not None or p['rows'] or p['watches']: raise ValueError('unknown is not zero')
    elif type(count) is not int or count != len(p['rows']) or unknown is None: raise ValueError('count mismatch')
    if p['availability'] == 'observed-empty':
        if count != 0 or unknown != 0 or any(v is None or generated - stamp(v) >= 1800 for v in p['polls'].values()):
            raise ValueError('unobserved empty')
    if p['availability'] == 'available' and not p['rows']: raise ValueError('missing rows')
    channels = []
    for row in p['rows']:
        if not isinstance(row, dict) or set(row) != {'name', 'channels'}: raise ValueError('private row field')
        if not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 128: raise ValueError('name bound')
        if not isinstance(row['channels'], list) or not 1 <= len(row['channels']) <= MAX_CHANNELS: raise ValueError('channels')
        for c in row['channels']:
            if not isinstance(c, dict) or set(c) != {'platform', 'viewers', 'observed_at', 'seen_at', 'destination'}:
                raise ValueError('private channel field')
            if c['platform'] not in network.INTERVALS: raise ValueError('platform')
            if c['viewers'] is not None and (type(c['viewers']) is not int or not 0 <= c['viewers'] <= 2147483647):
                raise ValueError('count is unknown or nonnegative integer')
            for key in ('seen_at', 'observed_at'):
                v = c[key]
                if type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= generated:
                    raise ValueError('observation timestamp')
            if now - c['seen_at'] >= 1800 or now - c['observed_at'] >= 86400: raise ValueError('expired observation')
            dest = network.safe_destination(c['destination'])
            if not dest or (c['platform'] == 'twitch') != dest.startswith('https://www.twitch.tv/'):
                raise ValueError('destination')
            channels.append(c)
    if len(channels) > MAX_CHANNELS: raise ValueError('channel bound')
    if not isinstance(p['watches'], dict) or len(p['watches']) > MAX_CHANNELS: raise ValueError('watch bound')
    for key, dest in p['watches'].items():
        if not re.fullmatch(TOKEN, key) or not network.safe_destination(dest) or dest not in {c['destination'] for c in channels}:
            raise ValueError('unapproved Watch destination')
    return p


class Viewer:
    def __init__(self, source_revision, digest, *, upstream_port=None, input_file=None):
        self.revision = revision(source_revision)
        if not re.fullmatch(r'[0-9a-f]{64}', digest): raise ValueError('artifact digest required')
        if (upstream_port is None) == (input_file is None): raise ValueError('choose one input')
        if digest != source_digest(): raise ValueError('viewer artifact mismatch')
        self.digest, self.upstream_port = digest, upstream_port
        self.input_file = Path(input_file) if input_file is not None else None

    def read(self):
        if self.input_file is not None:
            info = self.input_file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES: raise ValueError('bounded regular file required')
            fd = os.open(self.input_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError('regular file required')
                body = stream.read(MAX_BYTES + 1)
        else:
            # Fixed loopback peer/path, no proxy environment, redirects or arbitrary URL input.
            connection = http.client.HTTPConnection('127.0.0.1', self.upstream_port, timeout=2)
            try:
                connection.request('GET', '/projection')
                response = connection.getresponse()
                if response.status != 200: raise ValueError('projection unavailable')
                body = response.read(MAX_BYTES + 1)
            finally:
                connection.close()
        return validate(body, self.revision, self.digest)

    def page(self, packet):
        esc = html.escape
        if packet is None or packet['availability'] == 'unavailable':
            content = '<h2>Cache unavailable</h2><p>No observed population can be claimed. Retry after the runtime projection is available.</p>'
        else:
            # Escape first; recognize only the renderer's local Watch links, never general markdown/HTML.
            text = esc(packet['text'])
            def watch(match):
                label, key = match.groups()
                if key in packet['watches'] and self.input_file is None:
                    return f'<a href="/go/{key}" rel="noreferrer">{label}</a>'
                return label + (' (replay only)' if self.input_file is not None else ' (unavailable)')
            text = re.sub(r'\[(Watch on (?:Twitch|YouTube))\]\(/go/(' + TOKEN + r')\)', watch, text)
            text = re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', text)
            content = ''.join('<p>' + line + '</p>' for line in text.splitlines())
            content += '<p>Primary channels with an unknown audience count: ' + str(packet['unknown_primary_count']) + '.</p>'
            content += '<h2>Observation provenance</h2><p>Generated ' + esc(packet['generated_at']) + '</p><ul>'
            for platform, last in packet['polls'].items():
                content += '<li>' + network.SOURCE_NAMES[platform] + ': last successful poll ' + esc(last or 'unknown') + '</li>'
            content += '</ul>'
            for row in packet['rows']:
                for c in row['channels']:
                    content += '<p class="meta">' + esc(row['name']) + ' · ' + network.SOURCE_NAMES[c['platform']] + '<br>Observed ' + utc(c['observed_at']) + '<br>Presence recorded ' + utc(c['seen_at']) + '</p>'
        return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Pop1 Live — cache inspection</title><style>'
            ':root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0f0d16;color:#eceaf4;font:16px/1.55 "Bricolage Grotesque",system-ui,sans-serif}'
            'main{max-width:680px;margin:48px auto;padding:0 22px}h1{font-size:36px;line-height:1.15;margin:0 0 22px}h2{font-size:19px;margin:30px 0 8px}'
            'p{margin:12px 0}section{border-block:1px solid #2a2536;padding:10px 0 22px;overflow-wrap:anywhere}a{color:#b39dff;text-underline-offset:4px}a:hover{color:#eceaf4}'
            'a:focus-visible{outline:2px solid #8b5cf6;outline-offset:4px}.meta,footer{color:#c9c5d8;font-size:16px;overflow-wrap:anywhere}'
            '::selection{background:#7c4dff;color:white}@media(max-width:600px){main{margin:30px auto}}</style>'
            '<main><h1>Pop1 Live<br>Cache inspection</h1><p>Read-only cached observations, not a live population or public-launch approval. '
            'Watch links recheck listing eligibility before redirect; file replays cannot redirect.</p><section>' + content + '</section>'
            '<footer><p>Source revision ' + esc(self.revision) + '<br>Artifact digest ' + esc(self.digest) + '</p></footer></main></html>').encode()


def listener(port, *, projection=None, viewer=None):
    if (projection is None) == (viewer is None): raise ValueError('choose one surface')
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def reply(self, code, body=b'', location=None):
            self.send_response(code)
            self.send_header('Content-Type', 'application/json' if projection else 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, max-age=0')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            if location: self.send_header('Location', location)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.requestline.split()[1] != self.path: return self.reply(404)
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
                return self.reply(400)
            if projection:
                if self.path != '/projection': return self.reply(404)
                return self.reply(200, encode(projection.read()))
            if self.path != '/live' and not re.fullmatch('/go/' + TOKEN, self.path): return self.reply(404)
            try: packet = viewer.read()
            except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, RecursionError): packet = None
            if self.path == '/live':
                return self.reply(503 if packet is None or packet['availability'] == 'unavailable' else 200, viewer.page(packet))
            destination = packet['watches'].get(self.path[4:]) if packet and viewer.input_file is None else None
            if not network.safe_destination(destination): return self.reply(410)
            self.reply(303, location=destination)

        def reject(self): self.reply(405)
        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = reject
        def log_message(self, *args): pass  # No visitor/click log.
    # One bounded request at a time; no unbounded thread-per-visitor allocation.
    return HTTPServer(('127.0.0.1', port), Handler)


def start_runtime(runtime, source_revision, port):
    server = listener(port, projection=Projection(runtime, source_revision))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def configured_runtime(runtime, config):
    if not config.get('INSPECTION_PORT'): return None
    return start_runtime(runtime, config.get('INSPECTION_REVISION'), int(config['INSPECTION_PORT']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('export', 'viewer', 'digest'))
    parser.add_argument('--revision')
    parser.add_argument('--digest')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--upstream-port', type=int)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    if args.mode == 'digest':
        print(source_digest())
        return
    revision(args.revision)
    if args.mode == 'export':
        # Existing application cache only; never initialize/migrate or call Network.start().
        runtime = network.Network(os.environ.get('DATA_DIR', '/data'), lambda p, c: c, config={})
        print(encode(Projection(runtime, args.revision).read()).decode())
        return
    server = listener(args.port, viewer=Viewer(args.revision, args.digest,
        upstream_port=args.upstream_port, input_file=args.input))
    print('inspection listening on 127.0.0.1:' + str(server.server_port), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()

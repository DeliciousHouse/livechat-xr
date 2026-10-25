"""Synthetic cache + real HTTP only; never evidence of a production population."""
import asyncio
import copy
import http.client
import socket
import threading
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import network


REVISION = 'a' * 40


class Export(unittest.TestCase):
    def test_runtime_export_is_opt_in_without_provider_start(self):
        import inspection
        with tempfile.TemporaryDirectory() as data:
            net = network.Network(data, lambda p, c: c, config={})
            self.assertTrue(hasattr(inspection, 'configured_runtime'), 'missing opt-in runtime integration')
            self.assertIsNone(inspection.configured_runtime(net, {}))
            with mock.patch.object(network.Network, 'start', side_effect=AssertionError('start')):
                server = inspection.configured_runtime(net, {'INSPECTION_PORT': '0', 'INSPECTION_REVISION': REVISION})
            try:
                self.assertEqual(server.server_address[0], '127.0.0.1')
                self.assertFalse(net.enabled)
                self.assertFalse(net.store.path.exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_export_distinguishes_empty_from_unavailable_without_starting_providers(self):
        import inspection
        with tempfile.TemporaryDirectory() as data:
            net = network.Network(data, lambda p, c: c, config={})
            export = inspection.Projection(net, REVISION)
            self.assertEqual(export.read()['availability'], 'unavailable')
            self.assertFalse(net.store.path.exists())
            net.store.initialize()
            self.assertEqual(export.read()['availability'], 'unavailable')
            for p in network.INTERVALS: net.record_poll(p)
            with mock.patch.object(network.Network, 'start', side_effect=AssertionError('provider start')), \
                 mock.patch.object(network, 'request_json', side_effect=AssertionError('provider request')):
                packet = export.read()
            self.assertEqual(packet['availability'], 'observed-empty')
            self.assertEqual(packet['eligible_count'], 0)
            self.assertEqual(packet['source_revision'], REVISION)
            self.assertIn('No eligible small streams in the observed cache', packet['text'])
            self.assertFalse(net.enabled)


class Projection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = network.Network(self.tmp.name, lambda p, c: c, config={})
        self.net.store.initialize()
        self.now = time.time()

    def test_store_projection_is_public_and_uses_existing_eligibility(self):
        s = self.net.store
        self.assertTrue(hasattr(s, 'inspection_rows'), 'missing owning-Store sanitized projection')
        s.upsert_presence('twitch', [dict(platform_id='private-id', handle='synthetic',
            stream_id='synthetic-stream', title='not exported', viewers=2, game=network.GAME)], self.now)
        lid = s.channels()[0]['listing_id']
        self.assertEqual(s.inspection_rows(self.now)['rows'], [])
        s.set_listing(lid, opted_in=True, discord_id='private-owner')
        s.record_supplied_channel(lid, 'twitch', 'synthetic')
        s.bind_verified_channel(lid, 'twitch', 'synthetic', 'private-id')
        s.set_listing(lid, review_status='reviewed')
        rows = s.inspection_rows(self.now)['rows']
        self.assertEqual(rows, [{'name': 'synthetic', 'channels': [{'platform': 'twitch',
            'viewers': 2, 'observed_at': self.now, 'seen_at': self.now,
            'destination': 'https://www.twitch.tv/synthetic'}]}])
        self.assertNotIn('private', json.dumps(rows))
        self.assertEqual(s.inspection_rows(self.now + 1800)['rows'], [])
        s.set_listing(lid, opted_in=False)
        self.assertEqual(s.inspection_rows(self.now)['rows'], [])


class Boundary(unittest.TestCase):
    def setUp(self):
        import inspection
        self.inspection = inspection
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = network.Network(self.tmp.name, lambda p, c: c, config={})
        self.net.store.initialize()
        self.now = time.time()
        s = self.net.store
        s.upsert_presence('twitch', [dict(platform_id='private-platform', handle='synthetic',
            stream_id='fixture-stream', title='private-title', viewers=2, game=network.GAME)], self.now)
        self.lid = s.channels()[0]['listing_id']
        s.set_listing(self.lid, opted_in=True, discord_id='private-discord')
        s.record_supplied_channel(self.lid, 'twitch', 'synthetic')
        s.bind_verified_channel(self.lid, 'twitch', 'synthetic', 'private-platform')
        s.set_listing(self.lid, review_status='reviewed')
        # Explicitly synthetic display text; production profile validation is not relaxed.
        with s.connect() as db:
            db.execute('UPDATE listings SET ign=?,timezone=?,alerts_enabled=1,would_pay=1 WHERE id=?',
                ('<script>synthetic</script>&', 'private-zone', self.lid))
        for platform in network.INTERVALS: self.net.record_poll(platform, now=self.now)
        self.export = inspection.Projection(self.net, REVISION)
        self.upstream = self.serve(projection=self.export)
        self.viewer = inspection.Viewer(REVISION, self.export.digest, upstream_port=self.upstream.server_port)
        self.http = self.serve(viewer=self.viewer)

    def serve(self, **kwargs):
        server = self.inspection.listener(0, **kwargs)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return server

    def request(self, method='GET', path='/live', port=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', port or self.http.server_port, timeout=10)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally: connection.close()

    def test_real_projection_renderer_watch_withdrawal_and_no_provider_access(self):
        original_connect = socket.socket.connect
        attempts = []
        def loopback_only(sock, address):
            attempts.append(address)
            if address[0] not in ('127.0.0.1', '::1'): raise AssertionError('provider socket')
            return original_connect(sock, address)
        with mock.patch.object(socket.socket, 'connect', loopback_only), \
             mock.patch.object(network, 'request_json', side_effect=AssertionError('provider request')), \
             mock.patch.object(network.Network, 'start', side_effect=AssertionError('provider startup')), \
             mock.patch.object(network.Network, 'collect_twitch', side_effect=AssertionError('collector')), \
             mock.patch.object(network.Network, 'collect_youtube', side_effect=AssertionError('collector')):
            status, headers, page = self.request()
            self.assertEqual(status, 200)
            self.assertIn('Cache inspection', page)
            self.assertNotIn('<script>synthetic', page)
            self.assertIn('&lt;', page)
            self.assertIn('Twitch viewers: 2', page)
            self.assertNotIn('streams live', page)
            self.assertEqual(headers['Cache-Control'], 'no-store, max-age=0')
            packet = self.viewer.read()
            self.assertEqual(packet['eligible_count'], 1)
            serialized = json.dumps(packet)
            for value in ('private-platform', 'private-title', 'private-discord', 'private-zone', 'alerts_enabled', 'would_pay'):
                self.assertNotIn(value, serialized)
            key = next(iter(packet['watches']))
            self.assertIn('/go/' + key, page)
            status, headers, _ = self.request(path='/go/' + key)
            self.assertEqual((status, headers['Location']), (303, 'https://www.twitch.tv/synthetic'))
            self.net.store.set_listing(self.lid, opted_in=False)
            self.assertEqual(self.request(path='/go/' + key)[0], 410)
            self.assertEqual(self.viewer.read()['availability'], 'observed-empty')
            self.assertFalse(self.net.enabled)
        self.assertTrue(attempts)

    def test_method_path_query_and_body_boundaries(self):
        for path in ('/', '/admin', '/projection', '/live?x=1', '/live/', '/go/' + 'a' * 22 + '?url=https://evil.invalid',
                     '/%6cive', '//live', '/oauth', '/register', '/go/short'):
            with self.subTest(path=path): self.assertEqual(self.request(path=path)[0], 404)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE', 'CONNECT'):
            with self.subTest(method=method): self.assertEqual(self.request(method=method)[0], 405)
        self.assertEqual(self.request(body='not a GET body')[0], 400)
        self.assertEqual(self.request(path='/live', port=self.upstream.server_port)[0], 404)
        self.assertEqual(self.request(method='POST', path='/projection', port=self.upstream.server_port)[0], 405)

    def test_validation_bounds_provenance_and_offsite_refusal(self):
        i = self.inspection
        packet = self.export.read()
        self.assertEqual(packet['availability'], 'available')
        mutations = [
            lambda p: p.update(source_revision='b' * 40),
            lambda p: p.update(source_digest='b' * 64),
            lambda p: p.update(generated_at=i.utc(time.time() - i.MAX_AGE)),
            lambda p: p.update(generated_at=i.utc(time.time() + 60)),
            lambda p: p.update(generated_at='2026-02-30T00:00:00.000000Z'),
            lambda p: p.update(generated_at='2026-10-25'),
            lambda p: p.update(eligible_count=0),
            lambda p: p.update(text='x' * 6001),
            lambda p: p.update(rows=p['rows'] * 101),
            lambda p: p['rows'][0].update(discord_id='not allowed'),
            lambda p: p['rows'][0]['channels'][0].update(destination='https://evil.invalid/'),
            lambda p: p['rows'][0]['channels'][0].update(destination='https://www.twitch.tv/synthetic?token=x'),
            lambda p: p['rows'][0]['channels'][0].update(viewers=-1),
            lambda p: p['rows'][0]['channels'][0].update(seen_at=time.time() - 1800),
            lambda p: p['rows'][0]['channels'][0].update(observed_at=0),
            lambda p: p.update(watches={'a' * 22: 'https://evil.invalid/'}),
        ]
        for mutate in mutations:
            modified = copy.deepcopy(packet)
            mutate(modified)
            with self.subTest(packet=modified), self.assertRaises((ValueError, TypeError)):
                i.validate(i.encode(modified), REVISION, self.export.digest)
        for body in (b' ' * (i.MAX_BYTES + 1), b'{"schema":1,"schema":1}', b'{"schema":NaN}'):
            with self.assertRaises(ValueError): i.validate(body, REVISION, self.export.digest)
        for url in ('https://www.twitch.tv/synthetic@evil.invalid', 'https://www.youtube.com.evil.invalid/watch?v=abcdefghijk',
                    'https://www.youtube.com/watch?v=abcdefghijk&redirect=x', '//evil.invalid'):
            self.assertIsNone(network.safe_destination(url))

    def test_stale_unreviewed_unknown_optout_and_cache_loss(self):
        s = self.net.store
        with s.connect() as db: db.execute('UPDATE live_streams SET viewers=NULL')
        packet = self.viewer.read()
        self.assertEqual(packet['availability'], 'unavailable')
        self.assertIsNone(packet['eligible_count'])
        with s.connect() as db: db.execute('UPDATE live_streams SET viewers=2')
        s.set_listing(self.lid, review_status='pending')
        self.assertEqual(self.viewer.read()['rows'], [])
        s.set_listing(self.lid, review_status='reviewed')
        with s.connect() as db: db.execute('UPDATE live_streams SET seen_at=?', (self.now - 1800,))
        self.assertEqual(self.viewer.read()['eligible_count'], 0)
        self.net.sources['youtube']['last_success'] = 0
        self.assertEqual(self.viewer.read()['availability'], 'unavailable')
        self.net.store.path.unlink()
        self.assertEqual(self.request()[0], 503)
        self.assertFalse(self.net.store.path.exists())

    def test_source_rename_expiry_and_upstream_loss_fail_closed(self):
        packet = self.viewer.read()
        old = next(iter(packet['watches']))
        self.net.store.upsert_presence('twitch', [dict(platform_id='private-platform', handle='renamed',
            stream_id='fixture-stream', title='private-title', viewers=2, game=network.GAME)], time.time())
        self.assertEqual(self.request(path='/go/' + old)[0], 410)
        packet = self.viewer.read()
        key = next(iter(packet['watches']))
        self.assertEqual(self.request(path='/go/' + key)[1]['Location'], 'https://www.twitch.tv/renamed')
        with mock.patch.object(self.inspection.time, 'time', return_value=time.time() + 1801):
            self.assertEqual(self.request(path='/go/' + key)[0], 410)
            self.assertEqual(self.request()[0], 503)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.assertEqual(self.request(path='/go/' + key)[0], 410)
        self.assertEqual(self.request()[0], 503)

    def test_missing_presence_observation_is_unknown_not_observed_empty(self):
        with self.net.store.connect() as db: db.execute('UPDATE live_streams SET seen_at=0')
        packet = self.viewer.read()
        self.assertEqual(packet['availability'], 'unavailable')
        self.assertIsNone(packet['eligible_count'])

    def test_capacity_and_missing_observation_never_become_empty(self):
        with self.net.store.connect() as db: db.execute('UPDATE channels SET observed_at=0')
        self.assertEqual(self.viewer.read()['availability'], 'unavailable')
        with self.net.store.connect() as db: db.execute('UPDATE channels SET observed_at=?', (self.now,))
        channels = self.net.store.live_channels()
        with mock.patch.object(self.net.store, 'live_channels', return_value=channels * 201):
            self.assertEqual(self.export.read()['availability'], 'unavailable')
        with self.net.store.connect() as db: db.execute('UPDATE channels SET handle=?', ('unsafe?redirect=x',))
        self.assertEqual(self.viewer.read()['availability'], 'unavailable')

    def test_replay_requires_regular_bounded_file(self):
        i = self.inspection
        viewer = i.Viewer(REVISION, self.export.digest, input_file=self.tmp.name)
        with mock.patch.object(Path, 'open', side_effect=AssertionError('nonregular input opened')):
            with self.assertRaises(ValueError): viewer.read()

    def test_file_replay_is_never_current_watch_authority(self):
        i = self.inspection
        packet = self.export.read()
        path = Path(self.tmp.name) / 'sanitized.json'
        path.write_bytes(i.encode(packet))
        viewer = i.Viewer(REVISION, self.export.digest, input_file=path)
        http = self.serve(viewer=viewer)
        key = next(iter(packet['watches']))
        self.assertEqual(self.request(path='/go/' + key, port=http.server_port)[0], 410)
        self.assertIn('replay only', self.request(port=http.server_port)[2])
        with mock.patch.object(i.time, 'time', return_value=i.stamp(packet['generated_at']) + i.MAX_AGE):
            self.assertEqual(self.request(port=http.server_port)[0], 503)
        path.write_bytes(b'x' * (i.MAX_BYTES + 1))
        self.assertEqual(self.request(port=http.server_port)[0], 503)
        path.unlink()
        self.assertEqual(self.request(port=http.server_port)[0], 503)

    def test_secondary_unknown_stays_unknown_and_original_timestamps_survive(self):
        s = self.net.store
        s.upsert_presence('youtube', [dict(platform_id='private-youtube', handle='@synthetic', stream_id='abcdefghijk',
            title='private', viewers=None, game=network.GAME)], self.now)
        s.record_supplied_channel(self.lid, 'youtube', '@synthetic')
        s.bind_verified_channel(self.lid, 'youtube', '@synthetic', 'private-youtube')
        packet = self.viewer.read()
        self.assertIn('YouTube viewers: unknown', packet['text'])
        channel = packet['rows'][0]['channels'][1]
        self.assertIsNone(channel['viewers'])
        self.assertEqual((channel['seen_at'], channel['observed_at']), (self.now, self.now))
        self.assertEqual(set(packet['watches'].values()), {'https://www.twitch.tv/synthetic', 'https://www.youtube.com/watch?v=abcdefghijk'})


if __name__ == '__main__':
    unittest.main()

"""Offline view-only launcher safety contract; no provider or live QA calls."""
import http.client
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import qa_relay


class NetworkFixture(unittest.TestCase):
    def test_view_only_safety_and_owned_cleanup(self):
        server = qa_relay.server
        originals = {name: getattr(server, name) for name in
                     ("DATA", "regs", "discovery", "loop", "GA_ID", "DONATE_URL", "network_csrf")}
        urlopen = urllib.request.urlopen
        for scenario in ("normal", "delayed", "closed-dm"):
            with self.subTest(scenario=scenario), \
                    mock.patch.object(server.network.Network, "start", side_effect=AssertionError("must not start")), \
                    mock.patch.object(server, "discord_exchange", side_effect=AssertionError("no OAuth")), \
                    mock.patch.object(server, "discord_identify", side_effect=AssertionError("no identity")):
                with qa_relay.fixture(network_fixture=True, scenario=scenario) as srv:
                    self.assertEqual(srv.server_address[0], "127.0.0.1")
                    net, loop, data = server.discovery, server.loop, server.DATA
                    self.assertEqual(net.config, {})
                    self.assertFalse(net.enabled)
                    self.assertEqual(net.tasks, set())
                    self.assertTrue(loop.is_running())
                    self.assertTrue(net.store.path.is_file())
                    self.assertEqual(len(net.store.channels()), 1)
                    token = next(iter(server.regs))
                    profile = server.listing_profile(server.regs[token])
                    original_profile = dict(profile)
                    self.assertEqual(profile["ign"], "Synthetic streamer")
                    self.assertEqual(bool(profile["dm_closed"]), scenario == "closed-dm")
                    self.assertEqual(server.GA_ID, "")
                    self.assertEqual(server.DONATE_URL, "")
                    self.assertEqual(server.DISCORD_ID, "")
                    self.assertEqual(server.GOOGLE_ID, "")
                    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=3)
                    def request(path, method="GET"):
                        conn.request(method, path)
                        response = conn.getresponse()
                        return response.status, response.headers, response.read().decode()
                    for path in ("/", "/privacy", "/terms", "/m/" + token, "/admin?key=synthetic-view-only"):
                        code, headers, body = request(path)
                        self.assertEqual(code, 200, path)
                        self.assertIn("SYNTHETIC VIEW-ONLY", body)
                        self.assertIn("form-action 'none'", headers["Content-Security-Policy"])
                        self.assertIsNone(headers["Location"])
                        self.assertNotRegex(body, r'(?i)<form\b|<script\b|<link\b|\baction=|href="(?:https?:|//)')
                        self.assertNotIn("Listing storage is unavailable", body)
                        if path.startswith("/m/"):
                            self.assertNotIn("checking your channel automatically", body)
                            self.assertIn("fieldset:disabled", body)
                            self.assertIn("Pop1 Live listing", body)
                            self.assertIn("Listed when you", body)
                            self.assertIn("Synthetic streamer", body)
                            self.assertIn("Synthetic reviewed-listing copy", body)
                            self.assertNotIn("Channel ownership verified", body)
                            self.assertIn("<fieldset disabled", body)
                        if path.startswith("/admin"):
                            self.assertNotIn("Chat relay: running", body)
                            self.assertNotIn("restart the relay", body)
                            self.assertIn("Opt-ins: 1", body)
                            self.assertEqual("Twitch data stale" in body, scenario == "delayed")
                            self.assertEqual("DMs closed: 1 listing" in body, scenario == "closed-dm")
                    code, headers, body = request("/pretext.js")
                    self.assertEqual(code, 200)
                    self.assertIn("text/javascript", headers["Content-Type"])
                    forbidden = ("/health", "/register", "/recover", "/go/fixture", "/discord/callback",
                                 "/network/discord/callback", "/discord/interactions", "/stripe-webhook",
                                 "/__qa/reset", "/__qa/stop", "/admin", "/admin?key=wrong",
                                 "/m/not-a-fixture-registration", "/m/" + token + "?new=email",
                                 "/m/" + token + "/network", "/qa-payment/monthly", "/?action=reset")
                    with mock.patch.object(server.Handler, "do_GET", side_effect=AssertionError("production dispatch")), \
                            mock.patch.object(server.Handler, "do_POST", side_effect=AssertionError("production mutation")):
                        for path in forbidden:
                            self.assertEqual(request(path)[0], 404, path)
                        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "CONNECT", "TRACE", "BREW"):
                            for path in ("/", "/__qa/reset", "/__qa/stop", "/m/" + token + "/network"):
                                self.assertEqual(request(path, method)[0], 405, (method, path))
                    self.assertFalse(srv.stopped.is_set())
                    self.assertEqual(len(server.regs), 1)
                    self.assertEqual(server.listing_profile(server.regs[token]), original_profile)
                    for open_url in (urllib.request.urlopen, urllib.request.build_opener().open):
                        with self.assertRaisesRegex(RuntimeError, "external network disabled"):
                            open_url("https://example.invalid")
                    self.assertEqual(net._discord("POST", "/fixture", {}), (200, {"id": "synthetic-only"}))
                    conn.close()
                self.assertFalse(data.exists())
                self.assertTrue(loop.is_closed())
                self.assertFalse(srv.fixture_loop_thread.is_alive())
                self.assertFalse(srv.fixture_http_thread.is_alive())
                self.assertEqual(srv.fileno(), -1)
                self.assertEqual(net.tasks, set())
                self.assertEqual(net.io_tasks, set())
                self.assertIs(urllib.request.urlopen, urlopen)
                for name, original in originals.items():
                    self.assertIs(getattr(server, name), original, name)
        # Exceptions still clean up the owned resources and restore adapters.
        with self.assertRaisesRegex(RuntimeError, "abort fixture"):
            with qa_relay.fixture(network_fixture=True) as srv:
                data, loop = server.DATA, server.loop
                raise RuntimeError("abort fixture")
        self.assertFalse(data.exists())
        self.assertTrue(loop.is_closed())
        self.assertIs(server.regs, originals["regs"])
        with self.assertRaises(ValueError):
            with qa_relay.fixture(network_fixture=True, scenario="unknown"):
                self.fail("invalid startup scenario accepted")


if __name__ == "__main__":
    unittest.main()

"""Offline synthetic transport only; no real Discord credentials or requests."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import qa_discord as qa

HOOK_ID = "1000000000000000001"
CHANNEL = "1000000000000000002"
GUILD = "1000000000000000003"
MESSAGE = "1000000000000000004"
HOOK = "https://discord.com/api/webhooks/" + HOOK_ID + "/" + "x" * 68
ENV = dict(LIVECHAT_XR_QA_DISCORD_WEBHOOK=HOOK, LIVECHAT_XR_QA_DISCORD_GUILD_ID=GUILD,
           LIVECHAT_XR_QA_DISCORD_CHANNEL_ID=CHANNEL)


def encoded(value):
    return json.dumps(value).encode()


class Consumer(unittest.TestCase):
    def run_fixture(self, changes=None, env=None):
        message = dict(id=MESSAGE, content=qa.MARKER, webhook_id=HOOK_ID, channel_id=CHANNEL)
        responses = [(200, encoded(dict(guild_id=GUILD, channel_id=CHANNEL))),
                     (200, encoded(message)), (200, encoded(message)), (204, b""), (404, b"")]
        for index, response in (changes or {}).items():
            responses[index] = response
        calls = []

        def transport(method, url, payload=None):
            calls.append((method, url, payload))
            response = responses[len(calls) - 1]
            if isinstance(response, Exception):
                raise response
            return response

        result = qa.run(ENV if env is None else env, transport)
        self.assertEqual(set(result), {"fixture", "matched_scope", "posted", "readback", "deleted",
                                       "cleanup", "http_status", "error_code"})
        serialized = json.dumps(result)
        for secret in (HOOK, "x" * 68, GUILD, CHANNEL, MESSAGE, "private identity"):
            self.assertNotIn(secret, serialized)
        return result, calls

    def test_success(self):
        result, calls = self.run_fixture()
        self.assertEqual(result, dict(fixture="livechat-xr-discord-qa", matched_scope=True,
                                     posted=True, readback=True, deleted=True, cleanup=True,
                                     http_status=404, error_code="ok"))
        self.assertEqual([c[0] for c in calls], ["GET", "POST", "GET", "DELETE", "GET"])
        self.assertEqual(calls[1][1], HOOK + "?wait=true")
        self.assertEqual(calls[1][2], {"content": qa.MARKER, "allowed_mentions": {"parse": []}})
        self.assertEqual([c[1] for c in calls[2:]], [HOOK + "/messages/" + MESSAGE] * 3)

    def test_scope_and_config_fail_closed(self):
        for field in ("guild_id", "channel_id"):
            with self.subTest(field=field):
                scope = dict(guild_id=GUILD, channel_id=CHANNEL)
                scope[field] = "1000000000000000099"
                result, calls = self.run_fixture({0: (200, encoded(scope))})
                self.assertEqual(result["error_code"], "scope_mismatch")
                self.assertEqual([c[0] for c in calls], ["GET"])
        for key in ENV:
            for value in ("", "invalid", "123"):
                env = dict(ENV, **{key: value})
                result, calls = self.run_fixture(env=env)
                self.assertFalse(calls)
                self.assertFalse(result["matched_scope"])

    def test_strict_url(self):
        for url in (HOOK.replace("https:", "http:"), HOOK.replace("discord.com", "discord.com.evil"),
                    HOOK.replace("discord.com", "canary.discord.com"),
                    HOOK.replace("discord.com", "discord.com:443"),
                    HOOK.replace("discord.com", "user@discord.com"), HOOK + "?wait=true", HOOK + "#x",
                    HOOK + "/", HOOK + "\n", HOOK.replace("/api/", "/api/v10/"),
                    HOOK.replace("/api/", "/%61pi/"), HOOK.replace("x" * 68, "../escape")):
            with self.subTest(url=url):
                result, calls = self.run_fixture(env=dict(ENV, LIVECHAT_XR_QA_DISCORD_WEBHOOK=url))
                self.assertEqual(result["error_code"], "invalid_config")
                self.assertFalse(calls)

    def test_redirect_timeout_and_redaction(self):
        for response, error in (((302, encoded({"private identity": HOOK})), "scope_http"),
                                (TimeoutError(HOOK), "scope_transport"),
                                (ValueError("private identity " + HOOK), "scope_transport"),
                                ((200, HOOK.encode()), "scope_json"), ((200, b"[]"), "scope_json")):
            result, calls = self.run_fixture({0: response})
            self.assertEqual(result["error_code"], error)
            self.assertEqual(len(calls), 1)

    def test_ambiguous_post_never_retries_or_deletes_unknown_message(self):
        for response, error in ((TimeoutError(HOOK), "post_transport"), ((503, b""), "post_http"),
                                ((302, b""), "post_http"), ((200, b"broken"), "post_json"),
                                ((200, encoded(dict(id="../../other", content=qa.MARKER))), "post_identity")):
            result, calls = self.run_fixture({1: response})
            self.assertEqual(result["error_code"], error)
            self.assertEqual([c[0] for c in calls], ["GET", "POST"])
            self.assertFalse(result["cleanup"])

    def test_readback_failure_still_cleans_exact_created_message(self):
        for response, error in ((TimeoutError(HOOK), "readback_transport"), ((200, b"{}"), "readback_identity"),
                                ((200, b"bad"), "readback_json"), ((302, b""), "readback_http")):
            result, calls = self.run_fixture({2: response})
            self.assertEqual(result["error_code"], error)
            self.assertFalse(result["readback"])
            self.assertTrue(result["cleanup"])
            self.assertEqual([c[0] for c in calls], ["GET", "POST", "GET", "DELETE", "GET"])

    def test_cleanup_uncertainty(self):
        for changes, error in (({3: TimeoutError(HOOK), 4: TimeoutError(HOOK)}, "cleanup_transport"),
                               ({3: (403, b""), 4: (200, b"{}")}, "cleanup_http"),
                               ({4: TimeoutError(HOOK)}, "cleanup_transport"),
                               ({4: (200, b"{}")}, "cleanup_http")):
            result, calls = self.run_fixture(changes)
            self.assertEqual(result["error_code"], error)
            self.assertFalse(result["cleanup"])
            self.assertEqual(sum(c[0] == "POST" for c in calls), 1)
            self.assertEqual(sum(c[0] == "DELETE" for c in calls), 1)
            self.assertEqual(calls[-1][0], "GET")  # even ambiguous DELETE gets independent readback
        result, _ = self.run_fixture({3: TimeoutError(HOOK), 4: (404, b"")})
        self.assertTrue(result["cleanup"])
        self.assertFalse(result["deleted"])
        self.assertEqual(result["error_code"], "delete_transport")

    def test_native_transport_no_redirect_no_proxy_bounded_body(self):
        connection = mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 302
        response.read.return_value = b"private identity"
        with mock.patch.object(qa.http.client, "HTTPSConnection", return_value=connection) as factory:
            status, body = qa.request("GET", HOOK)
        factory.assert_called_once_with("discord.com", timeout=5)
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(status, 302)
        response.read.assert_called_once_with(65537)
        connection.close.assert_called_once()
        for body in (b"x" * 65537, TimeoutError(HOOK)):
            connection.reset_mock()
            response.read.side_effect = body if isinstance(body, Exception) else None
            response.read.return_value = body
            with mock.patch.object(qa.http.client, "HTTPSConnection", return_value=connection):
                with self.assertRaises(Exception):
                    qa.request("GET", HOOK)
            connection.close.assert_called_once()

    def test_main_receipt_only_and_no_arguments(self):
        output = io.StringIO()
        with mock.patch.object(qa.sys, "argv", ["qa_discord.py", HOOK]), \
                mock.patch.object(qa, "run") as run, contextlib.redirect_stdout(output):
            self.assertEqual(qa.main(), 1)
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error_code"], "arguments_forbidden")
        self.assertNotIn(HOOK, output.getvalue())
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(qa.sys, "argv", ["qa_discord.py"]), \
                mock.patch.object(qa, "run", side_effect=RuntimeError(HOOK)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(qa.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "internal_error")
        self.assertNotIn(HOOK, output.getvalue())
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

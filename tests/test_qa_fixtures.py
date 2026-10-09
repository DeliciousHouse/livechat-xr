"""Disposable launcher flow and Windows app isolation boundaries."""
import ctypes
import http.client
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "app")]


class RelayFixture(unittest.TestCase):
    def test_launcher_flow(self):
        import qa_relay
        with qa_relay.fixture() as srv:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)

            def request(path, **form):
                conn.request("POST" if form or path.endswith(("/test", "/delete")) else "GET", path,
                             urllib.parse.urlencode(form), {"Content-Type": "application/x-www-form-urlencoded"})
                r = conn.getresponse()
                return r.status, r.getheader("Location"), r.read().decode()

            form = dict(name="QA Fixture", email="qa@example.invalid", platform="tiktok", webhook="fixture-one")
            for channel in ("", "<bad>", "@qa_nonexistent"):
                code, _, body = request("/register", channel=channel, **form)
                self.assertEqual(code, 400)
                self.assertIn('value="QA Fixture"', body)
                self.assertNotIn("discord.com/api/webhooks/100", body)
            code, loc, _ = request("/register", channel="@qa_offline", **form)
            self.assertEqual(code, 303)
            manage = loc.split("?")[0]
            self.assertIn("Isolated simulated transport", request(manage)[2])
            self.assertIn("offline", request(manage)[2])
            code, again, _ = request("/register", channel="https://www.tiktok.com/@qa_lookup_error", **form)
            self.assertEqual((code, again), (303, loc))
            self.assertIn("couldn&#x27;t verify", request(manage)[2])
            form["webhook"] = "fixture-two"
            code, second, _ = request("/register", channel="qa_offline", **form)
            pending = second.split("?")[0]
            self.assertEqual(code, 303)
            body = request(pending)[2]
            self.assertIn("waiting for payment", body)
            self.assertNotIn("buy.stripe.com", body)
            self.assertIn('href="/qa-payment/monthly?', body)
            self.assertIn("No payment is collected", request("/qa-payment/monthly")[2])
            self.assertIn("Simulated test accepted", request(manage + "/test")[2])
            for path in (manage, pending):
                self.assertEqual(request(path + "/delete")[0], 200)
            self.assertEqual(request("/health")[2], "ok 0")
            conn.request("POST", "/__qa/reset", "")
            self.assertEqual(conn.getresponse().read(), b"reset: zero registrations")
            conn.request("POST", "/__qa/stop", "")
            self.assertEqual(conn.getresponse().read(), b"stopping isolated fixture")
            self.assertTrue(srv.stopped.is_set())
            conn.close()
            data = qa_relay.server.DATA
        self.assertFalse(data.exists())


@unittest.skipUnless(sys.platform == "win32", "Windows tray/registry boundary")
class PortableApp(unittest.TestCase):
    def test_isolation_settings_autostart_and_mutex(self):
        import chat
        import livechat_xr as app
        original = chat.DIR
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(chat, "DIR", original):
            alternate = Path(tmp) / "portable"
            with mock.patch.object(app, "App") as ui, mock.patch.object(app.winreg, "OpenKey") as registry, \
                    mock.patch.object(ctypes, "windll") as dll, mock.patch.object(app.logging, "basicConfig"), \
                    mock.patch.object(app, "RotatingFileHandler"):
                dll.kernel32.GetLastError.return_value = 0
                app.main(["--settings", "--data-dir", str(alternate)])
                self.assertEqual(chat.DIR, alternate.resolve())
                ui.assert_called_once_with(settings=True, portable=True)
                registry.assert_not_called()
                name = dll.kernel32.CreateMutexW.call_args.args[2]
                self.assertNotEqual(name, "LiveChatXR.single-instance")
                self.assertEqual(name, app.mutex_name(alternate))
                dll.kernel32.CloseHandle.assert_called_once()
            cfg = chat.load_config(portable=True)
            self.assertEqual(cfg["games"]["exes"], "LiveChatXR-QA-NotAGame.exe")
            obj = app.App.__new__(app.App)
            obj.portable, obj.cfg = True, cfg
            obj.settings = True
            obj.root, obj.icon, obj.runner = mock.Mock(), mock.Mock(), mock.Mock()
            cfg["chat"]["channel"] = "qa_configured"
            with mock.patch.object(obj, "open_settings") as settings:
                obj.run()
                settings.assert_called_once()
            with mock.patch.object(app.tk, "Toplevel"), mock.patch.object(app.tk, "StringVar"), \
                    mock.patch.object(app.tk, "BooleanVar"), mock.patch.object(app, "ttk"), \
                    mock.patch.object(app.winreg, "OpenKey") as registry:
                obj.win = None
                obj.open_settings()
                registry.assert_not_called()
                app.ttk.Checkbutton.return_value.state.assert_called_once_with(["disabled"])
            cfg["chat"]["channel"] = ""
            obj.vars, obj.platform = {}, mock.Mock()
            obj.platform.get.return_value = "twitch"
            obj.runner, obj.win = mock.Mock(), mock.Mock()
            with mock.patch.object(app, "set_autostart") as auto:
                obj.save()
                auto.assert_not_called()
            obj.test_banner()
            self.assertTrue((alternate / "banner.txt").exists())
            self.assertTrue((alternate / "config.ini").exists())
            self.assertFalse((alternate / "overlay.log").exists())
            for invalid in ("relative", str(alternate / "config.ini"), str(original)):
                with mock.patch.object(app, "App") as ui:
                    with self.assertRaises(SystemExit):
                        app.main(["--data-dir", invalid])
                    ui.assert_not_called()
            with mock.patch.object(app.tempfile, "TemporaryFile", side_effect=PermissionError("unwritable")), \
                    mock.patch.object(app, "App") as ui:
                with self.assertRaises(SystemExit):
                    app.main(["--data-dir", str(alternate)])
                ui.assert_not_called()


if __name__ == "__main__":
    unittest.main()

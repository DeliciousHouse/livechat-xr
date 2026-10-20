"""Approved branding resources, packaging and loopback HTTP; no user state or Discord traffic."""
import hashlib
import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "server")]
import chat
import server


class Brand(unittest.TestCase):
    def test_imported_manifest(self):
        brand = ROOT / "assets/brand"
        manifest = json.loads((brand / "manifest.json").read_text())
        for entry in manifest["files"]:
            data = (brand / entry["path"]).read_bytes()
            self.assertEqual(len(data), entry["bytes"], entry["path"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["path"])

    @unittest.skipUnless(sys.platform == "win32", "Windows Tk icons")
    def test_root_settings_and_tray_use_approved_art(self):
        import livechat_xr as app
        with mock.patch.object(app.tk, "Tk") as root, mock.patch.object(app.pystray, "Icon") as tray, \
                mock.patch.object(app.chat, "load_config", return_value=chat.load_config(portable=True)), \
                mock.patch.object(app.chat, "Runner"), mock.patch.object(app, "autostart_enabled", return_value=False):
            obj = app.App(portable=True)
            ico = str(app.brand_path("windows/LiveChatXR.ico"))
            root.return_value.iconbitmap.assert_called_once_with(default=ico)
            self.assertEqual(tray.call_args.args[1].size, (32, 32))
            with mock.patch.object(app.tk, "Toplevel") as settings, mock.patch.object(app.tk, "StringVar"), \
                    mock.patch.object(app.tk, "BooleanVar"), mock.patch.object(app, "ttk"):
                obj.open_settings()
                settings.return_value.iconbitmap.assert_called_once_with(ico)

    def test_source_and_frozen_resources(self):
        if sys.platform != "win32":
            self.skipTest("Windows tray app")
        import livechat_xr as app
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(app.brand_path("windows/LiveChatXR.ico"), ROOT / "assets/brand/windows/LiveChatXR.ico")
            with app.icon_image() as image:
                self.assertEqual(image.size, (32, 32))
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(ROOT), create=True):
            with app.icon_image() as image:
                self.assertEqual(image.size, (32, 32))
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(ROOT / "build/fixture"), create=True):
            self.assertEqual(app.brand_path("png/livechat-xr-32.png"), ROOT / "build/fixture/assets/brand/png/livechat-xr-32.png")
            with self.assertRaises(FileNotFoundError):
                app.icon_image()

    def test_native_ico_frames(self):
        from PIL import Image
        with Image.open(ROOT / "assets/brand/windows/LiveChatXR.ico") as icon:
            self.assertEqual(icon.ico.sizes(), {(n, n) for n in (16, 32, 48, 64, 128, 256)})
            for n in (16, 32):
                with Image.open(ROOT / f"assets/brand/png/livechat-xr-{n}.png") as png:
                    self.assertEqual(icon.ico.getimage((n, n)).convert("RGBA").tobytes(), png.convert("RGBA").tobytes())

    def test_static_get_bytes_types_and_paths(self):
        routes = {
            "/favicon.ico": ("favicon.ico", "image/vnd.microsoft.icon"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
            "/brand/header-dark.svg": ("header-dark.svg", "image/svg+xml"),
            "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
            "/brand/discord-avatar.png": ("discord-avatar.png", "image/png"),
        }
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=srv.serve_forever, daemon=True)
        worker.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)
            for route, (name, mime) in routes.items():
                with self.subTest(route=route):
                    conn.request("GET", route + "?v=1")
                    response = conn.getresponse()
                    data = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), mime)
                    expected = (ROOT / "assets/brand/web" / name).read_bytes()
                    self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(expected).digest())
                    self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            for route in ("/brand/../server.py", "/brand/%2e%2e/server.py", "/brand/source/build_brand.py", "/brand/header-dark.svg/../server.py", "/brand//header-dark.svg"):
                conn.request("GET", route)
                response = conn.getresponse()
                self.assertEqual(response.status, 404, route)
                response.read()
            with mock.patch.object(server.Path, "read_bytes", side_effect=FileNotFoundError):
                conn.request("GET", "/favicon.ico")
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                self.assertEqual(response.read(), b"Brand resource unavailable")
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            worker.join()
        html = server.page("fixture").decode()
        for route in ("/favicon.ico", "/favicon.svg", "/apple-touch-icon.png", "/brand/header-dark.svg"):
            self.assertIn(route, html)
        self.assertIn('alt="LiveChat XR"', html)

    def test_webhook_preserves_custom_avatar(self):
        with mock.patch.object(chat.urllib.request, "urlopen") as post:
            self.assertTrue(chat.post_discord("https://discord.com/api/webhooks/1/fixture", "@everyone fixture"))
        payload = json.loads(post.call_args.args[0].data)
        self.assertEqual(payload["username"], "LiveChat XR")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("avatar_url", payload)  # Existing custom avatars must not be overridden.

    def test_simulated_ui_preserves_static_bytes(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import qa_relay
        with qa_relay.fixture() as srv:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)
            try:
                for route, file, mime in (("/brand/header-dark.svg", "header-dark.svg", "image/svg+xml"),
                                          ("/favicon.ico", "favicon.ico", "image/vnd.microsoft.icon")):
                    conn.request("GET", route)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), mime)
                    self.assertEqual(response.read(), (ROOT / "assets/brand/web" / file).read_bytes())
            finally:
                conn.close()

    def test_packaging_contract(self):
        spec = (ROOT / "LiveChatXR.spec").read_text()
        self.assertIn('icon=str(brand / "windows/LiveChatXR.ico")', spec)
        self.assertIn('"assets/brand/png"', spec)
        self.assertIn('"assets/brand/windows"', spec)
        self.assertIn("pyinstaller --noconfirm --clean LiveChatXR.spec", (ROOT / ".github/workflows/build.yml").read_text())
        self.assertIn("COPY assets/brand/web ./assets/brand/web", (ROOT / "server/Dockerfile").read_text())
        installer = (ROOT / "installer/livechat-xr.iss").read_text()
        self.assertIn("SetupIconFile=..\\assets\\brand\\windows\\LiveChatXR.ico", installer)
        self.assertIn("UninstallDisplayIcon={app}\\LiveChatXR.exe", installer)

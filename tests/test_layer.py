"""Checks the built layer DLL outside any game: loader handshake + banner raster (writes tests/preview.png)."""
import ctypes as C
import math
import struct
import subprocess
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DLL = ROOT / "layer" / "livechat_xr_layer.dll"
W, H = 1024, 320


class LoaderInfo(C.Structure):
    _fields_ = [("structType", C.c_int), ("structVersion", C.c_uint32), ("structSize", C.c_size_t),
                ("minInterfaceVersion", C.c_uint32), ("maxInterfaceVersion", C.c_uint32),
                ("minApiVersion", C.c_uint64), ("maxApiVersion", C.c_uint64)]


class LayerRequest(C.Structure):
    _fields_ = [("structType", C.c_int), ("structVersion", C.c_uint32), ("structSize", C.c_size_t),
                ("layerInterfaceVersion", C.c_uint32), ("layerApiVersion", C.c_uint64),
                ("getInstanceProcAddr", C.c_void_p), ("createApiLayerInstance", C.c_void_p)]


@unittest.skipUnless(DLL.exists(), "build layer/livechat_xr_layer.dll first")
class Layer(unittest.TestCase):
    dll = None

    @classmethod
    def setUpClass(cls):
        cls.dll = C.CDLL(str(DLL))

    def test_handshake(self):
        li = LoaderInfo(1, 1, C.sizeof(LoaderInfo), 1, 1, 1 << 48, 1 << 48 | 1 << 32)
        req = LayerRequest(2, 1, C.sizeof(LayerRequest))
        self.assertEqual(self.dll.xrNegotiateLoaderApiLayerInterface(C.byref(li), b"XR_APILAYER_LIVECHATXR_banner", C.byref(req)), 0)
        self.assertEqual(req.layerInterfaceVersion, 1)
        self.assertTrue(req.getInstanceProcAddr and req.createApiLayerInstance)

    def test_rejects_unsupported_loader(self):
        li = LoaderInfo(1, 1, C.sizeof(LoaderInfo), 2, 2, 1 << 48, 1 << 48)
        req = LayerRequest(2, 1, C.sizeof(LayerRequest))
        self.assertNotEqual(self.dll.xrNegotiateLoaderApiLayerInterface(C.byref(li), b"x", C.byref(req)), 0)

    def test_short_lines_and_prefixed_gifts(self):
        for text in ("", "x", "xy", "xyz", "🎵", "▶️", "🎵 x", "🟣 x", "▶️ x",
                     "🎁 Gift", "🎵 🎁 Gift", "🟣 🎁 Gift", "▶️ 🎁 Gift"):
            with self.subTest(text=text):
                buf = (C.c_uint32 * (W * H))()
                self.dll.livechatxr_render_test(text.encode(), buf)
                gold = any((p & 0xFF) > 200 and (p & 0xFF) - (p >> 16 & 0xFF) > 120 for p in buf)
                self.assertEqual(gold, "🎁" in text)
                if text and "🎁" not in text:
                    self.assertTrue(any(min(p & 0xFF, p >> 8 & 0xFF, p >> 16 & 0xFF) > 240 for p in buf))

    def test_viewer_box_hugs_the_right_and_draws_platform_logos(self):
        VW, VH = 1024, 96
        rgb = lambda p: (p & 0xFF, p >> 8 & 0xFF, p >> 16 & 0xFF)

        def draw(text):
            buf = (C.c_uint32 * (VW * VH))()
            self.dll.livechatxr_viewers_test(text.encode(), buf)
            return buf

        self.assertFalse(any(draw("")), "no live platform: nothing drawn")
        buf = draw("tiktok 12\ntwitch 3\nyoutube 1")
        mid = buf[VH // 2 * VW:(VH // 2 + 1) * VW]
        self.assertEqual(mid[0] >> 24, 0, "left of the box is transparent")
        self.assertGreater(mid[-1] >> 24, 200, "box reaches the right edge")
        colors = list(map(rgb, (p for p in buf if p >> 24 > 200)))
        self.assertTrue(any(r > 200 and g < 60 and b < 60 for r, g, b in colors), "YouTube red")
        self.assertTrue(any(100 < r < 190 and g < 110 and b > 200 for r, g, b in colors), "Twitch purple")
        self.assertTrue(any(r < 120 and g > 200 and b > 200 for r, g, b in colors), "TikTok cyan")
        one = draw("twitch 1")
        width = lambda b: sum(1 for p in b[VH // 2 * VW:(VH // 2 + 1) * VW] if p >> 24)
        self.assertLess(width(one), width(buf), "the box shrinks to its text")

    def follow(self, st, yaw_deg, pitch_deg=0.0, pos=(0.0, 0.0, 0.0), dt=1 / 90, snap=False, follow_deg=20.0):
        pose = (C.c_float * 7)()
        f = self.dll.livechatxr_follow_test
        f.argtypes = [C.POINTER(C.c_float)] + [C.c_float] * 6 + [C.c_int, C.c_float, C.POINTER(C.c_float)]
        f(st, math.radians(yaw_deg), math.radians(pitch_deg), *pos, dt, int(snap), follow_deg, pose)
        return list(pose)

    def test_banner_holds_still_until_you_look_away_then_glides_back(self):
        # 2026-10-14: the VIEW-locked banner "jumps around" when the game drops frames and the runtime reprojects.
        st = (C.c_float * 7)()
        pose = self.follow(st, 0, snap=True)
        self.assertAlmostEqual(pose[6], -1.0, places=4)  # 1 m straight ahead (-Z) after a snap
        for _ in range(90):  # a second of small head motion stays inside the 20 degree dead zone
            self.follow(st, 12, 4, pos=(0.05, 0.0, 0.02))
        self.assertAlmostEqual(math.degrees(st[0]), 0.0, places=3)
        self.assertEqual(st[6], 0.0, "not moving")
        self.follow(st, 45)  # turn well past it
        self.assertEqual(st[6], 1.0, "starts gliding")
        for _ in range(180):  # two seconds later it has settled in front of the new heading
            pose = self.follow(st, 45)
        self.assertLess(abs(math.degrees(st[0]) - 45), 2.5)
        self.assertEqual(st[6], 0.0, "settled")
        self.assertAlmostEqual(pose[4], -math.sin(st[0]), places=3)  # the quad sits along the new heading
        self.assertAlmostEqual(pose[6], -math.cos(st[0]), places=3)
        self.assertAlmostEqual(pose[0] ** 2 + pose[1] ** 2 + pose[2] ** 2 + pose[3] ** 2, 1.0, places=4)

    def test_follow_wraps_the_short_way_in_both_directions(self):
        for start, target in ((170, -170), (-170, 170)):
            with self.subTest(start=start, target=target):
                st = (C.c_float * 7)()
                self.follow(st, start, snap=True, follow_deg=10)
                self.follow(st, target, follow_deg=10)
                # A signed first step rejects a frozen anchor and a long-way rotation.
                step = math.remainder(math.degrees(st[0]) - start, 360)
                self.assertGreater(step * (1 if start > 0 else -1), 0.1)
                self.assertLess(abs(step), 5)
                for _ in range(180):
                    self.follow(st, target, follow_deg=10)
                error = math.remainder(math.degrees(st[0]) - target, 360)
                self.assertLess(abs(error), 2.5)
                self.assertEqual(st[6], 0.0, "settled at the target, not the starting heading")

    def test_zero_follow_submits_view_locked_quads(self):
        result = subprocess.run([str(ROOT / "layer" / "runtime_test.exe")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_raster(self):
        buf = (C.c_uint32 * (W * H))()
        self.dll.livechatxr_render_test("🎁 BigFan sent Galaxy x3\nViewer One: nice shot!\nSecondPerson: what gun is that 🔫".encode(), buf)
        alpha = [p >> 24 for p in buf]
        self.assertGreater(alpha[0], 150, "top-left is inside the dark box")
        self.assertEqual(alpha[(H - 1) * W], 0, "rows below the text are transparent")
        rgb = lambda p: (p & 0xFF, p >> 8 & 0xFF, p >> 16 & 0xFF)
        top, rest = buf[:W * 80], buf[W * 90:]
        self.assertTrue(any(r > 200 and r - b > 120 for r, g, b in map(rgb, top)), "gift line (first row) is gold")
        self.assertTrue(any(min(rgb(p)) > 240 for p in rest), "comment lines are white")
        rows = b"".join(b"\0" + b"".join(bytes(int(ch * (p >> 24) / 255 + 128 * (1 - (p >> 24) / 255)) for ch in (p & 0xFF, p >> 8 & 0xFF, p >> 16 & 0xFF))
                                         for p in buf[y * W:(y + 1) * W]) for y in range(H))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        (ROOT / "tests" / "preview.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    unittest.main()

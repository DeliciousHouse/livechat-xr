"""Checks the built layer DLL outside any game: loader handshake + banner raster (writes tests/preview.png)."""
import ctypes as C
import struct
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

    def test_raster(self):
        buf = (C.c_uint32 * (W * H))()
        self.dll.livechatxr_render_test("Viewer One: nice shot!\nSecondPerson: what gun is that 🔫\n+2 more".encode(), buf)
        alpha = [p >> 24 for p in buf]
        self.assertGreater(alpha[0], 150, "top-left is inside the dark box")
        self.assertEqual(alpha[(H - 1) * W], 0, "rows below the text are transparent")
        self.assertGreater(max(p & 0xFF for p in buf), 240, "text pixels are white")
        rows = b"".join(b"\0" + b"".join(bytes([int((p & 0xFF) * (p >> 24) / 255 + 128 * (1 - (p >> 24) / 255))] * 3)
                                         for p in buf[y * W:(y + 1) * W]) for y in range(H))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        (ROOT / "tests" / "preview.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    unittest.main()

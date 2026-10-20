# PyInstaller source of truth; paths are relative to this spec, never the caller's cwd.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH)
brand = root / "assets/brand"
datas = [(str(brand / "png"), "assets/brand/png"),
         (str(brand / "windows"), "assets/brand/windows")]
binaries, hiddenimports = [], collect_submodules("EulerApiSdk")
for package in ("TikTokLive", "TikTokLiveProto"):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden

a = Analysis([str(root / "app/livechat_xr.py")], pathex=[str(root / "app")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LiveChatXR",
          console=False, icon=str(brand / "windows/LiveChatXR.ico"))
coll = COLLECT(exe, a.binaries, a.datas, name="LiveChatXR")

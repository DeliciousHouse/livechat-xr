# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

LiveChat XR shows Twitch / TikTok LIVE chat inside PC VR games (OpenXR + D3D11), and optionally relays it to a
Discord webhook so a standalone Quest gets it as notifications. Windows-only for the PC parts; the relay server runs
anywhere (Docker).

## Commands

Windows, Python 3.12, VS 2022 Build Tools (C++), Inno Setup 6. Use the repo's `.venv`.

```bat
layer\build.cmd                                         :: builds layer\livechat_xr_layer.dll (finds vcvars64 itself)
.venv\Scripts\pip install -r app\requirements.txt pyinstaller
.venv\Scripts\python -m unittest discover -s tests -v   :: all tests (stdlib unittest, no pytest)
.venv\Scripts\python -m unittest tests.test_chat.Batch.test_gifts_first -v   :: single test
.venv\Scripts\python app\livechat_xr.py                 :: run the tray app from source
.venv\Scripts\python app\chat.py twitch <channel> <discord webhook>         :: headless relay, no tray/overlay
set DATA_DIR=.\data& .venv\Scripts\python server\server.py   :: relay server (needs app\ on PYTHONPATH or chat.py beside it)
```

Packaging (same as CI): `pyinstaller --noconfirm --windowed --name LiveChatXR --collect-all TikTokLive --collect-all TikTokLiveProto --collect-submodules EulerApiSdk --paths app app\livechat_xr.py`, then `ISCC.exe /DAppVersion=x.y.z installer\livechat-xr.iss`. `dist\LiveChatXR\LiveChatXR.exe --check` is the packaging smoke test (exits 0 if bundled deps import). `*.spec` is gitignored; the flags above are the source of truth.

Relay image: `docker build -f server/Dockerfile -t livechat-xr-relay .` (build context must be the repo root: it copies `app/chat.py`).

CI (`.github/workflows/build.yml`, windows-latest) builds the layer, runs tests, packages, smoke-tests, and builds the installer on every push/PR; a `v*` tag publishes a GitHub release (a `-` in the tag marks it prerelease).

## Architecture

Three processes, coupled only through files in `%LOCALAPPDATA%\LiveChatXR\` (and the shared `app/chat.py` module):

- **`app/chat.py`** is the core, shared by both the tray app and the relay server. Chat sources (`twitch()` = anonymous IRC with tags; `tiktok()` = unofficial `TikTokLive` lib, lazily imported) emit lines; `relay()` collects them and calls `on_batch(text)` once per window (`[banner] seconds`), with `batch()` putting gift lines first so they never fold into "+N more". `Runner` runs this on a background thread with its own event loop. Also owns `config.ini` defaults/load/save, `write_banner()` and `post_discord()`.
- **`app/livechat_xr.py`** is the tray app (pystray + a hidden Tk root). Tray callbacks run on pystray's thread and must go through `self.ui` queue, which `pump()` drains on the Tk thread. Single instance via a named mutex. Writes `config.ini` on first run because the layer reads it.
- **`layer/livechat_xr_layer.cpp`** is an implicit OpenXR API layer DLL loaded into games. On negotiation it reads `config.ini` (`[games] exes` allow-list: other processes get pure pass-through; `[banner]` placement), watches `banner.txt` on a thread, rasterizes with GDI into a 1024x320 texture, and adds a head-locked (VIEW space) quad layer in `xrEndFrame`. D3D11 only: any failure or non-D3D11 session calls `Disable()` and the layer becomes pass-through for the rest of the process. Logs to `overlay.log`. Kill switch `LIVECHATXR_DISABLE` is declared in `livechat_xr_layer.json`.
- **`server/server.py`** is the hosted Discord relay (stdlib `http.server`, no framework): sign-up form, per-registration manage token, Stripe Payment Link billing (first `FREE_SLOTS` free; then `pending` until a signed `/stripe-webhook`), optional Discord OAuth / Google sign-in / GA, `/admin?key=`. Each registration runs a `chat.relay()` task posting to its webhook. All config is env vars at the top of the file. State persists as JSON in `DATA_DIR` (`registrations.json`, `stats.json`) plus rotating `relay.log`.

Cross-component contracts to keep in sync:

- The `🎁 ` prefix (`chat.GIFT`) is what makes the layer draw a line in gold (`GIFT` in the .cpp).
- `config.ini` keys in `chat.DEFAULTS` / `FIELDS` are read directly by the layer via `GetPrivateProfileStringW`. The layer reads config only at game start.
- `banner.txt` and `config.ini` are always written via temp file + `os.replace` so the layer never sees a partial write; keep that.
- The installer registers the layer under **HKLM** `SOFTWARE\Khronos\OpenXR\1\ApiLayers\Implicit` (some loaders, e.g. Meta's OVRPlugin, ignore HKCU), hence it needs admin.

## Tests

- `tests/test_chat.py`: batching, Twitch IRC parsing, relay loop (mocked, no network).
- `tests/test_layer.py`: loads the built DLL with ctypes, checks loader handshake and calls the exported `livechatxr_render_test` to verify raster colors; writes `tests/preview.png` (gitignored) for eyeballing. **Skipped if the DLL isn't built**, so run `layer\build.cmd` first when touching the layer.
- `tests/test_server.py`: spins up `server.Handler` on a random port with `check_webhook`, `start`/`stop` and `chat.post_discord` monkeypatched; covers input validation at the trust boundary (webhook URL regex, channel names), billing, and admin escaping. Tests import `app/` and `server/` by inserting them into `sys.path`.

## Conventions

- Keep it dependency-light: the app needs only `TikTokLive`, `pystray`, `Pillow`; the server only `TikTokLive` (stdlib for HTTP, Discord, Stripe signature checks). Versions are pinned in `app/requirements.txt` and `server/Dockerfile`; bump both together.
- Discord posts always set `allowed_mentions: {"parse": []}` so chat text can't ping anyone. The webhook URL is the user's credential: never render it in pages or logs.
- User-facing behaviour changes generally need a README update (it is the user manual).

## Design System
Read DESIGN.md before visual or UI work: it defines the fonts, colors, spacing, and
aesthetic direction. Ask the user before departing from it. When reviewing or QA-ing
UI, flag code that doesn't match DESIGN.md.

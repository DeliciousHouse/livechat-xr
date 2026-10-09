# How to develop and test LiveChat XR

Run the existing Python tests and native layer tests without installing an overlay into your games.

## Prerequisites

- A checkout of this repository. Run all commands from its root.
- Windows x64 for the tray app and native DLL tests; CI uses Windows and Python 3.12.
- Python 3.12, or [uv](https://docs.astral.sh/uv/getting-started/installation/) to select that interpreter.
- Visual Studio 2022 Build Tools with the C++ x64 tools for the DLL. The script expects the BuildTools edition at its default path unless `cl` is already on PATH.
- Inno Setup 6 only when building an installer; see the [release/deploy runbook](deployment.md).

Examples below use Windows Command Prompt, not PowerShell or WSL. In a shell that injects an unrelated PYTHONPATH, clear it for these project processes first. Do not copy a global Hermes Python environment into this project.

## Set up and run tests

1. Create the Python 3.12 environment and install the same app/packaging dependencies as CI:

   ```bat
   uv venv --python 3.12 .venv
   uv pip install --python .venv/Scripts/python.exe -r app/requirements.txt "pyinstaller==6.*"
   ```

   TikTokLive is pinned at 7.0.1, pystray at 0.19.5; Pillow is constrained to 11 or newer. The Docker relay only installs TikTokLive because it does not run the tray UI.

2. Build the layer, then run the tests:

   ```bat
   layer\build.cmd
   .venv\Scripts\python.exe -m unittest discover -s tests -v
   ```

   The layer build writes a DLL in `layer/` but does not register it or start a game. Without that DLL, three native tests skip; that is not a complete pass. The raster test writes `tests/preview.png`. Tests mock stream providers, Discord and Stripe; they do not contact your viewers or charge a card.

3. Check the app's imports without launching the tray or connecting chat:

   ```bat
   .venv\Scripts\python.exe app\livechat_xr.py --check
   ```

   Success exits with code 0, normally without text. This checks source imports, not an installed app or a working headset overlay. The packaged executable has the same `--check` smoke entrypoint in CI.

For documentation-only changes, you can run the smaller link/discoverability check:

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -p test_docs.py -v
```

There is no repo `package.json` or `npm run verify` script. Use unittest plus the native/build/packaging gates in [build.yml](../.github/workflows/build.yml), not a fabricated npm gate.

## How to run an empty local relay

Use a new data directory, an unused port and no production environment file. The development server binds all interfaces and has no TLS; keep it on a trusted local machine and do not expose it publicly.

In Command Prompt from the repo root:

```bat
set PYTHONPATH=app
set DATA_DIR=.local-relay-data
set PORT=13310
.venv\Scripts\python.exe server\server.py
```

In your browser, connect to loopback address `127.0.0.1` on port `13310` using HTTP. Open paths `/`, `/privacy` and `/health`. With empty data, health is `ok 0`; `/admin` returns 404 unless explicitly configured. Stop with Ctrl+C. Close the dedicated command window to discard its environment. Never copy Dora's private environment or registrations into this directory.

Do not submit a real webhook just to check rendering. HTTP registration, update/delete, identity and payment transitions are exercised by `tests/test_server.py` with mocked transport. A successful test is not proof of real Discord delivery, Stripe activation or headset notifications.

## Build and release

The [workflow](../.github/workflows/build.yml) builds the DLL, tests, packages the Windows tray app with PyInstaller, smoke-tests the bundled dependencies, and compiles the Inno installer. Installer source is [livechat-xr.iss](../installer/livechat-xr.iss). `v*` tag builds publish a release; tags containing `-` are pre-releases. Do not push a release tag just to test docs.

The CI/CD card owns exact packaging/release commands, SHA256 publication, Dora deployment and rollback. Use the [canonical runbook](deployment.md) for those procedures rather than duplicating them here.

## Troubleshooting development

- Native tests skipped: build `layer/livechat_xr_layer.dll` first on Windows x64. Do not treat Linux as a platform for loading this Windows DLL.
- `cl` missing: install the C++ Build Tools or run in an x64 developer command environment. Successful compilation matters more than an incidental tool-discovery warning.
- Module imports resolve to unrelated packages: clear inherited PYTHONPATH, use this checkout's `.venv`, then set PYTHONPATH to `app` only for the relay command.
- Packaging fails on TikTok dependencies: follow the workflow's collect options, including TikTokLive, TikTokLiveProto and EulerApiSdk; an imports-only source check does not replace the packaged smoke test.
- Test socket/resource warnings: report separately from failures. Do not change unrelated production code in a docs PR.

## Verification notes

The documentation preparation run compiled the native DLL and passed the existing 41 tests with no native skips. It exercised the commands above in a clean Python 3.12 environment, an empty temporary local relay, and the docs link check. `npm run verify` was attempted and returned **Missing script: verify**; it is unavailable, not passing.

`images/pc-settings.png` captures the actual Tk Settings UI with an isolated data path, without starting chat, saving settings, writing registry values or launching a game. `images/relay-signup.png` captures the public empty signup page, without signing in or submitting. These are setup illustrations, not install/transport/headset acceptance proof. The public page may differ from a local deployment depending on configured Google/Discord integrations.

Retained newcomer QA on 2026-10-09 did not demonstrate completed signup/manage/delete, paid activation, PC installation/first-run delivery or standalone in-game pop-ups. This docs change addresses its price, Quest installation-source and release-download wording gaps; it does not turn that failed acceptance verdict into a pass. Real billing, installer elevation, notifications, backups, restore and secret rotation are not rehearsed by the docs test.

See [CONTRIBUTING](../CONTRIBUTING.md), [reference](reference.md), [architecture](architecture.md) and [user guide](user-guide.md).

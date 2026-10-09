# Isolated QA fixtures

These commands run from the repository root. They do not deploy the relay, install the app, or register an OpenXR layer. Never use an owner's webhook, manage link, profile directory, game executable or installer for this journey.

## Relay: real templates, simulated transport

Python's standard library is sufficient; no credentials or third-party packages are needed:

```bat
python tools\qa_relay.py --port 13301
```

Open `http://127.0.0.1:13301/`. The listener is always IPv4 loopback. `--port 0` chooses an unused port and prints only the public fixture address. Every launch creates an empty disposable directory; environment-provided data directories, billing links, OAuth identities and analytics are ignored. The production Handler, validation, redirects, templates, persistence and free/pending logic are reused. Only the launcher substitutes profile lookup, Discord validation/posting and chat start/stop. Outbound urllib requests fail closed. The page's CSP also prevents external scripts and cross-origin form submissions.

Use the prefilled typed identity `QA Fixture` / `qa@example.invalid` (not Google sign-in). The non-secret webhook field handles are `fixture-one` and `fixture-two`. The launcher constructs their inert, syntax-valid values internally and replaces them with handles in retained-value HTML; do not extract or screenshot webhook values or manage tokens. Obtain manage pages through the normal registration redirect, not a printed token. Capture the page content only, excluding browser address bars and raw HTML.

TikTok scenarios (also accepted as full `https://www.tiktok.com/@...` profile URLs):

| Channel | Deterministic result |
| --- | --- |
| `qa_nonexistent` | Profile lookup returns False; exact missing-account inline error; form values retained; nothing saved |
| `qa_offline` | Profile exists but simulated offline; accepted without connecting chat |
| `qa_lookup_error` | Lookup raises TimeoutError at the reviewed `check_channel` boundary; real fail-open registration and explanatory note |

Other syntactically valid names simulate existing offline profiles. None of these are real account-existence checks. Blank fields use native browser validation; invalid channel/webhook input still runs production server validation. The simulated test button sends nothing to Discord. A simulation notice stays visible on every HTML page. This fixture is not Discord, Google, Stripe, production deployment or Quest notification proof.

### Journey and cleanup

1. Submit a blank channel; correct it to invalid syntax, then `@qa_nonexistent`. Check inline errors and retained identity/webhook fields.
2. Correct it to `@qa_offline` with `fixture-one`. Follow the normal redirect; the first registration is free.
3. Use **Set up another channel**, submit `https://www.tiktok.com/@qa_lookup_error` with `fixture-one`. Confirm the same manage page is updated in place and shows the inconclusive-lookup note. Use **Send test message**; the result explicitly says no Discord message was sent.
4. Register `qa_offline` with `fixture-two`. The second registration is pending, not running. Plan links lead only to `/qa-payment/monthly` or `/qa-payment/yearly` on this listener and say no payment is collected. They do not upgrade the registration.
5. Delete both registrations through **Stop and delete**. `/health` must read `ok 0`.

Safe reset (only this process's temporary data):

```bat
curl -X POST http://127.0.0.1:13301/__qa/reset
```

Stop with Ctrl+C in the launcher terminal, or:

```bat
curl -X POST http://127.0.0.1:13301/__qa/stop
```

Orderly shutdown removes the entire launcher-owned temporary directory. Restart always starts empty. If the process is forcibly killed, remove only its generated `livechatxr-qa-*` directory after verifying its PID is stopped; never remove a supplied or production data directory. The launcher does not print tokens, webhook values or submitted form data.

## Windows: visible alternate-profile Settings

Install source dependencies in a repository-local virtual environment, not the installed application's directory:

```bat
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r app\requirements.txt
.venv\Scripts\python.exe app\livechat_xr.py --settings --data-dir "C:\absolute\disposable\LiveChatXR-QA"
```

With a reviewed portable build, the equivalent command is:

```bat
LiveChatXR.exe --settings --data-dir "C:\absolute\disposable\LiveChatXR-QA"
LiveChatXR.exe --check
```

Use a new absolute directory separate from `%LOCALAPPDATA%\LiveChatXR`. Relative paths, files, unwritable directories and paths overlapping the installed profile are rejected before runner/UI creation, without fallback. Existing alternate profiles retain their own config. The mutex is scoped to the resolved, case-normalized directory, so the installed app can stay running; a second process on that same alternate directory is refused. Normal invocation retains the original single-instance mutex and autostart behavior.

An empty alternate profile shows first-run Settings and defaults Games to `LiveChatXR-QA-NotAGame.exe`, not Pop1. **Start with Windows** is disabled; opening/saving these Settings never reads or writes the installed HKCU Run value. Config, banner and rotating app logs stay in the alternate directory. The app does not register any layer; registration remains installer-only. An installed layer continues to use its installed data, not this alternate banner.

For no-network PC evidence, leave Channel and both optional credential fields empty. Change a numeric setting, click **Send test banner**, then **Save**. Verify only the disposable directory has `config.ini`, `banner.txt`, `app.log`; quit only the fixture tray process. Restart with the same command and verify the saved setting. `--settings` also opens Settings for configured profiles (covered by the Windows boundary regression); do not configure a real chat source merely to demonstrate that branch. A configured profile starts its ordinary chat runner, so only do that within an independently authorized transport test.

Compare hashes of owner app-data files (never display their contents), HKCU Run and HKCU/HKLM OpenXR registry snapshots before/after. Check the installed PID remains alive. Do not touch game settings or run a game. Quit the fixture's own tray icon before removing only its disposable data directory. Do not quit the owner's tray app, elevate, install, register HKLM layers or interact with Pop1.

This proves alternate-profile settings/persistence and banner-file output, not in-headset overlay rendering, actual transport, installer/UAC or HKLM installation behavior.

## Regression gate and build handoff

```bat
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python app\livechat_xr.py --check
```

`tests/test_qa_fixtures.py` covers one real launcher HTTP journey (including shutdown/reset) and one Windows isolation boundary (portable mutex, configured Settings, autostart avoidance, invalid/unwritable paths, config/banner output). Existing native-layer tests require a built DLL locally; the Windows CI build compiles it and runs the full suite plus packaged `--check` before producing its archive/installer artifact. This Python repository has no `npm run verify` script.

Consume only the exact reviewed commit's CI build artifact; do not execute the installer for fixture testing. The source archive contains this stdlib relay launcher. Q/QA should record the source commit and CI run/artifact ID, use these fixture commands, and retain actual transport/headset proof as separate acceptance work. Source delivery alone does not authorize deployment or convert simulated transport into live product readiness.

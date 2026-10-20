# Isolated QA fixtures

These commands run from the repository root. They do not deploy the relay, install the app, or register an OpenXR layer. Never use an owner's webhook, manage link, profile directory, game executable or installer for this journey.

## Relay: real templates, simulated transport

Use Python 3.12 with `PyNaCl==1.6.0` and `tzdata==2026.5` (the current server imports
discovery and IANA timezones). No credentials or TikTok/app packages are needed:

```bat
python tools\qa_relay.py --port 13301
```

Open `http://127.0.0.1:13301/`. The listener is always IPv4 loopback. `--port 0` chooses an unused port and prints only the public fixture address. Every launch creates an empty disposable directory; environment-provided data directories, billing links, OAuth identities and analytics are ignored. The production Handler, validation, redirects, templates, persistence and founder/free/paid logic are reused. Only the launcher substitutes profile lookup, Discord validation/posting and chat start/stop. Outbound urllib requests fail closed. The page's CSP also prevents external scripts and cross-origin form submissions.

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
2. Correct it to `@qa_offline` with `fixture-one`. Follow the normal redirect; the first registration is a founder (the fixture has one founder slot).
3. Use **Set up another channel**, submit `https://www.tiktok.com/@qa_lookup_error` with `fixture-one`. Confirm the same manage page is updated in place and shows the inconclusive-lookup note. Use **Send test message**; the result explicitly says no Discord message was sent.
4. Register `qa_offline` with `fixture-two`. The second registration is free, with one simulated channel active. Add Twitch/YouTube to see extra channels waiting for upgrade. Plan links lead only to `/qa-payment/monthly` or `/qa-payment/yearly` on this listener and say no payment is collected. They do not upgrade the registration.
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

## Synthetic view-only network fixture

This is a separate opt-in mode of the launcher, not the production server/image:

```bat
python tools\qa_relay.py --network-fixture --scenario normal --port 0
python -m unittest discover -s tests -p test_qa_network_fixture.py -v
```

`normal`, `delayed` and `closed-dm` are the only startup scenarios; restart with a
different option to change them. No runtime fault, reset or stop endpoints exist.
The console prints synthetic manage/admin paths, not real identity credentials.
Every HTML response labels itself SYNTHETIC VIEW-ONLY. The seeded streamer and
linked, reviewed channel association are fabricated presentation data, not
onboarding, channel-control, founder-review or Discord-delivery proof.

Only GET `/`, `/privacy`, `/terms`, `/pretext.js`, the internally seeded manage
path, and `/admin?key=synthetic-view-only` reach production dispatch. No other
query variants are accepted. All non-GET methods are rejected before dispatch,
including HEAD/OPTIONS and unknown verbs. All other routes return 404, including
register/recover/Watch redirects, both OAuth callbacks, Discord interactions,
Stripe, network actions and the relay fixture's reset/stop routes. Forms become
disabled fieldsets; links outside the exact read allowlist lose their href.
Scripts, external fonts/preconnect and refresh metadata are removed. CSP denies
scripts, form actions, network resources, framing and base overrides. The font
fallback is intentional for offline use; production styling/templates are unchanged.

Network uses literal empty config, initialized disposable SQLite, a fixture-local
fake `_discord`, and its own loop. It never calls `Network.start`, collectors,
OAuth exchange, `Web.connect` or production main. Disabled discovery/missing-config
notices on admin are expected, not a request to supply credentials. `delayed`
adds synthetic Twitch staleness; `closed-dm` adds one closed-DM listing. Fake
transport output stays in-process and is not delivery evidence. Billing, donation,
OAuth, Google and analytics config are blanked; no environment credentials are
passed into Network. Both urllib urlopen and opener dispatch fail closed.

Exit only through context exit / Ctrl+C: shutdown and join the owned listener,
stop owned Network tasks, drain async work, close its loop/SQLite connections,
restore patched adapters/state, then remove only the owned temporary directory.
Do not embed this process-global patch harness alongside a production server or
run concurrent fixtures in one Python process. The safety regression exercises
all scenarios, initialized listing rendering, denied routes/verbs, urllib egress,
unchanged listing state, normal/exception cleanup and restoration.

### QA artifact, dependencies and bind/isolation contract

`tools/qa-network-manifest.json` is the deliberate source-package allowlist and
dependency contract. Package only its `files` from the exact reviewed commit into
a distinct `LiveChatXR-Network-ViewOnly-<commit>.zip`, preserving relative paths;
include a generated receipt with that commit and each file's SHA256. Do not use
the normal portable app, installer, production Dockerfile/image, production state
volume, credentials or user profiles. The QA archive includes its launcher,
server imports, pretext, chat module, safety test, manifest and this runbook.
Install only the manifest's pinned direct dependencies into a dedicated Python
3.12 environment before running the packaged safety command above. PyNaCl's
normal transitive CFFI dependencies are installed by pip; the receipt records
resolved versions. Dependency installation is not fixture/provider traffic.

The sole bind remains IPv4 `127.0.0.1`, including `--port 0`; there is no bind
override. Container-loopback does NOT support ordinary host port publishing.
No container package/runtime or Dora provisioning is delivered or authorized.
Any later container choice needs a reviewed fixture-only bind interface (default
loopback), explicit no-egress network isolation and host `127.0.0.1` publication;
never infer those guarantees from this source package. Dora's later receipt must
include the reviewed commit/artifact, safety result, manifest/receipt and approved
runtime isolation mapping. Dev-lead contract review plus canonical review/merge
remain outstanding until the respective lanes act; source tests do not accept F,
change E/F1/G or establish phone/Quest/operator custody.

### Bounded capture source (not execution approval)

`tools/qa_capture.py` and its mocked checks are packaged as source only. See
[the capture contract](qa-capture.md) for the exact fixed layout, reviewed-plan
schema, bounds, receipts and separate runtime approval prerequisites. Do not run
the capture wrapper, browser or sandbox on the strength of this source archive.

## Private Discord transport consumer (Q only)

`tools/qa_discord.py` is separate from the simulated relay. It has no CLI arguments
and needs only Python's standard library. Offline regression command:

```bat
python -m unittest discover -s tests -p test_qa_discord.py -v
```

Q owns provisioning, enrollment, installation and any live invocation on controller
`t_e69bce14`. Source/tests alone are not live transport or launch acceptance. Do not
use a production webhook, copy registrations, or grant another profile access.

After sole-reviewer merge and green exact-head CI, Q installs the reviewed source
`tools/qa_discord.py` in a fixed, controller-owned directory and pins its commit/hash.
No app dependencies, installer, elevation, gateway restart or fleet change is needed
to run this standalone consumer. Q verifies the private test guild/channel separately.
The fixed process inherits exactly these inputs from Q's approved deployment:

| Environment key | Source |
| --- | --- |
| `LIVECHAT_XR_QA_DISCORD_WEBHOOK` | Approved machine-service secret; never argv, config or logs |
| `LIVECHAT_XR_QA_DISCORD_GUILD_ID` | Exact non-secret allowlisted private test guild ID |
| `LIVECHAT_XR_QA_DISCORD_CHANNEL_ID` | Exact non-secret allowlisted private test channel ID |

For approved Secrets Manager enrollment, Q uses the existing writer with the value
already in its environment (never paste it into a command or retrieve it into output):

```bat
C:/Users/bkam/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe C:/Users/bkam/AppData/Local/hermes/scripts/bws_write.py LIVECHAT_XR_QA_DISCORD_WEBHOOK --value-env LIVECHAT_XR_QA_DISCORD_WEBHOOK
```

Proposed launch contract, not an installed broker target: fixture handle
`livechat-xr-discord-qa`; executable Q's approved Python; sole argument the fixed
absolute installed `qa_discord.py` path; no caller-controlled arguments, shell,
destination, text, operations or environment overrides. Bind the inherited secret
to this consumer only, and bind both non-secret IDs in Q-owned configuration. Retain
the protected target map and existing per-use approval controls if Q registers a
trusted consumer; do not edit those maps or expand profile access from this runbook.
Missing registration/secret plumbing stays with Q, not a manual credential request.

The consumer strictly accepts only unversioned `https://discord.com/api/webhooks/`
URLs, checks metadata against both IDs before any write, and never follows redirects
or uses environment proxies. Five HTTP operations maximum, five-second socket
timeouts, bounded 64 KiB response bodies, no retries. It posts one fixed synthetic
message with mentions disabled, checks the returned identity/content, independently
reads that exact message, deletes only that ID, then requires a 404 readback.
Even a failed readback triggers cleanup of the known test-created ID. An ambiguous
DELETE still gets independent readback; `deleted` records acknowledged DELETE,
whereas `cleanup` records confirmed absence. An ambiguous POST or invalid POST
identity cannot safely identify a message to delete: cleanup remains false and
there is no retry, history enumeration or broad deletion. Q must investigate the
private fixture out of band before deciding on another approved run.

Stdout is one redacted JSON receipt and exit status is zero only for `error_code=ok`.
It contains fixture handle, matched-scope/posted/readback/deleted/cleanup booleans,
last observed HTTP status (null when none), and a static error code. Before any
POST attempt, `cleanup=true` means no message was created; after a POST attempt it
means independent 404 proof only. Never retain request URLs, bodies, identity data,
tokens or exception text. Share only this receipt plus reviewed commit/CI evidence.
This does not prove Google/OAuth, Stripe, Quest/headset, five-minute delivery or
canonical UI acceptance, and changes no production relay state.

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

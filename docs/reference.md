# LiveChat XR reference

For setup, use the [user guide](user-guide.md). For design rationale, read [architecture](architecture.md). This reference describes the checked-in implementation, not every deployed version.

## PC settings

Stored in `%LOCALAPPDATA%\LiveChatXR\config.ini`. The Python reader overlays saved values on these defaults. The layer reads the same file when its loader handshake first runs in a chosen game process.

| Section / key | Type and default | Effect |
|---|---|---|
| chat / platform | string, `twitch` | Settings offers `twitch` and `tiktok`; relay selects TikTok only for `tiktok` (case-insensitive), otherwise Twitch |
| chat / channel | string, empty | TikTok handle (leading `@` stripped); Twitch channel (leading `#` stripped, lowercased). Use a bare handle in the PC app, not a profile URL |
| chat / tiktok_sign_api_key | string, empty | Optional TikTokLive signing key; sets `SIGN_API_KEY` in the app process |
| chat / discord_webhook | string, empty | Optional Discord destination in addition to the PC banner |
| games / exes | string, `PopulationONE.exe` | Exact executable basenames, case-insensitive. Commas separate entries; layer also accepts semicolons |
| banner / seconds | float, `7` | Batch interval and display duration, seconds; Settings requires greater than zero |
| banner / max_lines | integer, `3` | Lines selected per batch; Settings requires greater than zero |
| banner / up | float, `0.22` | Metres above eye line; zero and negative values allowed |
| banner / distance | float, `1.0` | Metres forward; Settings requires greater than zero |
| banner / width | float, `0.62` | Metres wide; Settings requires greater than zero |

Settings validation is not a general-purpose config-file schema: hand-edited values bypass its checks. Use normal finite numbers. Placement, game selection and layer duration require a game restart. Saving restarts the app's chat connection. **Start with Windows** controls the current user's `LiveChatXR` registry Run entry; it is not an INI option.

The tray offers **Settings…**, **Send test banner**, **Open data folder**, **Quit**. Settings offers **Save** and **Send test banner**. Test sends a local banner and, if configured, a Discord post without requiring a live stream. Unsaved Settings fields do not apply to that test.

Files: `config.ini` (contains optional secrets), `banner.txt` (latest batch), `app.log` and `app.log.1` (rotating 1 MiB), `overlay.log` (layer log, cleared if over 1 MiB at a subsequent handshake). Atomic replacement prevents partial config/banner reads. Do not upload the data folder without checking it for secrets and chat text.

The manifest names `XR_APILAYER_LIVECHATXR_banner`. Installer registers it under the 64-bit machine-wide implicit OpenXR layer key. `LIVECHATXR_DISABLE=1` disables it through the manifest; restart affected games after changing the environment. Only OpenXR / D3D11 sessions draw; failures disable drawing and pass frames through. Texture is 1024 × 320; long text can be clipped. This is not a chat-history viewer.

## Shared chat behavior

### Configuration example

The default overlay-only config can keep secrets empty:

```ini
[chat]
platform = twitch
channel =
tiktok_sign_api_key =
discord_webhook =
[games]
exes = PopulationONE.exe
[banner]
seconds = 7
max_lines = 3
up = 0.22
distance = 1.0
width = 0.62
```

Set a real channel in Settings before connecting. This blank-channel example is safe to save but does not read a stream.

`app/chat.py` supplies `relay(platform, channel, on_batch, status, seconds=7, max_lines=3, sign_api_key="")`, an async coroutine used by both app and server. `on_batch(text)` receives nonempty windows; `status(text)` receives connection changes.

- Normal streamer comments are skipped. TikTok gift combos emit their final count. Twitch Bits and selected sub/resub/gift notices are supported; mass gifts suppress their individual notices.
- Gift lines sort before normal comments, stably within each group. At most `max_lines` lines are selected, followed by `+N more` when needed. Even gifts can exceed that limit; not every gift is guaranteed visible.
- Twitch uses anonymous TLS IRC, reconnecting after errors in 10 seconds. No Twitch password or key is requested.
- TikTok uses unofficial TikTokLive 7.0.1 and its signing service. Normal retries are 60 seconds; five consecutive account-not-found failures raise that to 1800 seconds. Connection, an offline result or a different error resets the count. Account-not-found can also mean no LIVE permission.
- `post_discord(webhook, text)` returns a bool, truncates content to 2000 characters, sets `allowed_mentions.parse` to an empty list, and times out after 10 seconds. Failed/rate-limited posts are dropped, not queued for replay.

## Relay configuration

Environment is read at import/startup. Restart after configuration changes. No secret values belong in documentation or issue reports.

| Variable | Default | Meaning |
|---|---|---|
| DATA_DIR | `data` (`/data` in Docker) | Persistent files |
| PORT | `13300` | HTTP port; server binds all interfaces |
| MAX_REGS | `50` | Hard ceiling including pending registrations |
| FREE_SLOTS | `5` | Current free-registration capacity |
| PUBLIC_URL | empty | Public origin for manage links and Discord callback; trailing slash stripped |
| PAY_MONTHLY | empty | Monthly Stripe Payment Link |
| PAY_YEARLY | empty | Yearly Stripe Payment Link |
| BILLING_URL | empty | Stripe customer-portal login URL |
| STRIPE_WEBHOOK_SECRET | empty | Signing secret; empty rejects Stripe events |
| DISCORD_CLIENT_ID | empty | Enables Connect Discord when set |
| DISCORD_CLIENT_SECRET | empty | Discord OAuth exchange secret |
| GOOGLE_CLIENT_ID | empty | Enables Google identity prefill/verification |
| GA_ID | empty | Enables GA4 on public home/privacy/manage pages |

Empty payment links do not create a free fallback: a full free pool still yields pending registrations. Configure billing before advertising paid service. Google is optional identity verification, not a stored account or an email-login system. Manual email input sends no verification email.

## Relay HTTP interface

Responses are HTML except simple health/Stripe text responses; there is no general JSON registration API.

| Method / path | Inputs and result |
|---|---|
| GET `/` | Signup form |
| POST `/register` | URL-encoded name, email, platform, channel, webhook; optional `google` credential and `via=discord` or `via=webhook`. 303 to manage on success; 400 validation error; 503 new-registration capacity reached |
| GET `/discord/callback` | One-use state and code, valid for 900 seconds; exchanges Discord authorization for the selected channel's webhook; 400 on failure |
| GET `/m/{token}` | Private manage page; 404 absent/deleted registration |
| POST `/m/{token}/test` | Posts a test even for a pending registration; 404 inactive link. “Test sent” is not a delivery receipt |
| POST `/m/{token}/delete` | Deletes registration and in-memory stats, stops runner. Does not cancel Stripe billing |
| GET `/privacy` | Stored data, analytics and deletion policy |
| GET `/health` | 200, `ok` plus registration count; liveness only, not end-to-end delivery |
| GET `/admin` | Protected by configured `ADMIN_KEY` query credential; unset or wrong key returns 404 |
| POST `/stripe-webhook` | Signed JSON event, max 512 KiB; 400 bad signature, 200 processed/ignored event |

`ADMIN_KEY` is empty by default; then admin is disabled. Keep its private access link in approved secret storage, not browser screenshots, reports or shell arguments.

Registration constraints: name truncated to 60 characters, email lowercased and syntax-checked (local part 1–64, domain part 1–190, final suffix at least two letters), TikTok handle 2–24 letters/digits/underscore/dot, Twitch 3–25 letters/digits/underscore. Leading `@` and supported TikTok/Twitch profile URLs normalize on the **server only**. Discord webhook must be HTTPS on discord.com or discordapp.com (including ptb/canary), numeric ID 15–22 digits and token 40–100 word/hyphen characters, then pass a Discord lookup. Ordinary POST bodies over 8192 bytes return 413. Manage-path tokens must be 20–64 word/hyphen characters.

TikTok signup checks profile hydration even when offline. A missing/unavailable account is rejected; network failures or inconclusive profile responses allow signup with a warning. Twitch signup checks syntax only. Reusing exactly the same webhook preserves token, plan, subscription and creation time, updating identity/channel fields. Free slots are counted from current registrations, not a lifetime signup counter. Deleting a free registration frees capacity. New paid-eligible entries remain `pending` until a signed `checkout.session.completed` with `payment_status=paid` and matching `client_reference_id` changes them to `paid`; free entries remain free. `customer.subscription.deleted` returns a matching entry to pending and stops it. The server does not handle every Stripe billing event, such as payment failures.

Signatures use HMAC-SHA256 with a 300-second timestamp tolerance. This is not a durable event-ID replay ledger. Persistent files are `registrations.json`, `stats.json` (saved on changes at 30-second intervals), rotating `relay.log`/`.1`, and backup timestamp files `last_backup`, `last_offsite`. Tokens, webhooks and subscription IDs in registration data are private. Admin shows counts, status, failures, recent logs and backup age, refreshing in 30 seconds; ages beyond 36 hours warn.

## Command interface

- `app/livechat_xr.py` without arguments launches the Windows tray app. Exact argument `--check` imports bundled TikTok events and exits without starting UI/chat.
- `app/chat.py` without arguments uses the local config for a headless runner. Its alternate three-argument form accepts platform, channel and webhook, but puts the webhook in process arguments; prefer protected config instead. This is not the hosted relay.
- `server/server.py` starts the HTTP relay and restores all non-pending registrations, staggering startup by 3 seconds.

See [development](development.md) for exercised local commands and [operations](operations.md) for the hosted service.

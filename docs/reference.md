# LiveChat XR reference

For setup, use the [user guide](user-guide.md). For design rationale, read [architecture](architecture.md). This reference describes the checked-in implementation, not every deployed version.

## PC settings

Stored in `%LOCALAPPDATA%\LiveChatXR\config.ini`. The Python reader overlays saved values on these defaults. The layer reads the same file when its loader handshake first runs in a chosen game process.

| Section / key | Type and default | Effect |
|---|---|---|
| chat / tiktok | string, empty | TikTok handle (leading `@` stripped). Use a bare handle in the PC app, not a profile URL |
| chat / twitch | string, empty | Twitch channel (leading `#` stripped, lowercased). Set one or both; chat from both shares the banner |
| chat / platform, chat / channel | legacy | Pre-0.2 single-platform pair. On load the app moves a set `channel` into `tiktok` or `twitch` (by `platform`, case-insensitive) and blanks it |
| chat / tiktok_sign_api_key | string, empty | Optional TikTokLive signing key; sets `SIGN_API_KEY` in the app process |
| chat / discord_webhook | string, empty | Optional Discord destination in addition to the PC banner |
| chat / follow_ask | string, `follow for the next sword-only run` | TikTok only. Banner line (prefixed `» `) shown once per viewer on their second message when TikTok does not report them as a follower (`follow_info.follow_status` 1 or 2); unknown status is asked. One ask per 90 seconds overall. Empty turns asks off; `sessions.jsonl` is still written |
| games / exes | string, `PopulationONE.exe` | Exact executable basenames, case-insensitive. Commas separate entries; layer also accepts semicolons |
| banner / seconds | float, `7` | Batch interval and display duration, seconds; Settings requires greater than zero |
| banner / max_lines | integer, `3` | Lines selected per batch; Settings requires greater than zero |
| banner / up | float, `0.22` | Metres above eye line; zero and negative values allowed |
| banner / distance | float, `1.0` | Metres forward; Settings requires greater than zero |
| banner / follow | float, `20` | Degrees you can look away before the banner glides back in front of you. The banner is anchored in the room, so it stays steady when the game drops frames; `0` locks it to your view every frame (the old behaviour). Read by the headset layer at game start; not shown in Settings |
| banner / width | float, `0.62` | Metres wide; Settings requires greater than zero |

Settings validation is not a general-purpose config-file schema: hand-edited values bypass its checks. Use normal finite numbers. Placement, game selection and layer duration require a game restart. Saving restarts the app's chat connection. **Start with Windows** controls the current user's `LiveChatXR` registry Run entry; it is not an INI option.

The tray offers **Settings…**, **Send test banner**, **Open data folder**, **Quit**. Settings offers **Save** and **Send test banner**. Test sends a local banner and, if configured, a Discord post without requiring a live stream. Unsaved Settings fields do not apply to that test.

Files: `config.ini` (contains optional secrets), `banner.txt` (latest batch), `app.log` and `app.log.1` (rotating 1 MiB), `sessions.jsonl` (one JSON line per TikTok LIVE connection: `start`, `end` (UTC ISO 8601), `channel`, `peak_viewers` (max `RoomUserSeqEvent.total`, or `null`), `new_follows` (distinct followers from `FollowEvent`), `asks_shown`; append-only, never rotated), `overlay.log` (layer log, cleared if over 1 MiB at a subsequent handshake). Atomic replacement prevents partial config/banner reads. Do not upload the data folder without checking it for secrets and chat text.

The manifest names `XR_APILAYER_LIVECHATXR_banner`. Installer registers it under the 64-bit machine-wide implicit OpenXR layer key. `LIVECHATXR_DISABLE=1` disables it through the manifest; restart affected games after changing the environment. Only OpenXR / D3D11 sessions draw; failures disable drawing and pass frames through. Texture is 1024 × 320; long text can be clipped. This is not a chat-history viewer.

## Shared chat behavior

### Configuration example

The default overlay-only config can keep secrets empty:

```ini
[chat]
tiktok =
twitch =
tiktok_sign_api_key =
discord_webhook =
follow_ask = follow for the next sword-only run
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

`app/chat.py` supplies `relay(platform, channel, on_batch, status, seconds=7, max_lines=3, sign_api_key="", channels=None, follow_ask=None, on_session=None, webhook="")`, an async coroutine used by both app and server. `channels={"tiktok": "@a", "twitch": "b"}` reads both platforms into the same windows; without it, `platform`/`channel` is a single source. `on_batch(text)` receives nonempty windows; `status(text)` receives connection changes, joined with ` · ` per platform when there are two. Optional `on_session(row)` receives one connection-local tally at a connected source's existing offline transition; callback failures do not interrupt chat.

- Normal streamer comments are skipped. TikTok gift combos emit their final count. Twitch Bits and selected sub/resub/gift notices are supported; mass gifts suppress their individual notices.
- `follow_ask=None` (the hosted relay) disables the PC follow-ask Session; the PC app passes `chat / follow_ask`. The server independently enables relay tallies with `on_session`.
- Follow-ask lines sort first, then gifts, then normal comments, stably within each group. At most `max_lines` lines are selected, followed by `+N more` when needed. A single ask survives gift overflow when `max_lines` is at least 1. If asks alone exceed that limit, later asks also fold into `+N more`; not every gift or ask is guaranteed visible in that case.
- Twitch uses anonymous TLS IRC, reconnecting after errors in 10 seconds. No Twitch password or key is requested.
- TikTok uses unofficial TikTokLive 7.0.1 and its signing service. Normal retries are 60 seconds; five consecutive account-not-found failures raise that to 1800 seconds. Connection, an offline result or a different error resets the count. Account-not-found can also mean no LIVE permission.
- `post_discord(webhook, text)` is the low-level single-attempt HTTP helper: it returns a bool, caps content at 2000 characters, disables mentions and times out after 10 seconds. `DiscordQueue` is used by hosted and PC chat delivery: `relay(..., on_lines=...)` supplies every line before banner truncation. It sends up to five lines / 2000 UTF-16 units per post, limits attempts to 25 per rolling minute and retries failures (429 honors Retry-After or JSON retry_after). Only unsent backlog older than 60 seconds is collapsed. Original-line counters `lines_received`, `lines_posted`, `lines_dropped` and `dropped_backlog` persist per registration in stats.json; service tags/summaries do not count as chat. Counts are cumulative, not guessed per-platform stream totals. Queues are in memory and discarded on stop/restart; one registration owns each webhook. Setup/test and account notices use the low-level helper.

Discord message types include chat batches, setup/test messages and session summaries. PC TikTok summaries are sent after the local session row is written, only with a configured webhook, and also written once to the banner: `Stream ended @channel - 1h42m - peak 9 viewers - 2 new follows - 1 follow asks shown`. Stopping/restarting the PC app or a connection drop also closes its existing Session. Unknown (`null`) fields are omitted rather than displayed.

The hosted relay appends `DATA_DIR/sessions.jsonl` with `registration_id`, `platform`, `channel`, UTC `start`/`end`, `comments`, `distinct_chatters`, `gifts`, `peak_viewers` and `new_follows`, and queues the matching summary separately from chat windows using the same Discord rate budget. `registration_id` is the private registration token: protect this file like `registrations.json`. No viewer identifiers or chat text are stored. Counts cover only observed non-owner comments; gifts count finalized gift/support events (one TikTok combo or Twitch mass-gift notice is one event, not its quantity or monetary value). YouTube backlog and repeated message IDs are excluded. Peak viewers and follows are `null` outside TikTok. Tallies reset on each connection; retries, cancellation and waiting while never connected do not produce hosted summaries. TikTok and YouTube have offline transitions; Twitch anonymous IRC does not, so it is tallied but cannot produce an end summary without an additional stream-status source. Persistence/posting failures are logged generically and do not stop forwarding. The append-only session file is not rotated or automatically purged on registration deletion.

## Relay configuration

Environment is read at import/startup. Restart after configuration changes. No secret values belong in documentation or issue reports.

| Variable | Default | Meaning |
|---|---|---|
| DATA_DIR | `data` (`/data` in Docker) | Persistent files |
| PORT | `13300` | HTTP port; server binds all interfaces |
| MAX_REGS | `50` | Hard ceiling including founder, free and paid registrations |
| FREE_SLOTS | `5` | Lifetime founder slots; later users get one channel free |
| PUBLIC_URL | empty | Public origin for manage links and Discord callback; trailing slash stripped. Required for Connect Discord: without it the button is hidden and an error is logged at startup. Keep it in the Dora env file, since the deploy controller passes only `--env-file` |
| DONATE_URL | empty | Set = free for everyone (all channels) and the pages ask for donations at this URL |
| PAY_MONTHLY | empty | Monthly Stripe Payment Link |
| PAY_YEARLY | empty | Yearly Stripe Payment Link |
| BILLING_URL | empty | Stripe customer-portal login URL |
| STRIPE_WEBHOOK_SECRET | empty | Signing secret; empty rejects Stripe events |
| DISCORD_CLIENT_ID | empty | Enables Connect Discord when set |
| DISCORD_CLIENT_SECRET | empty | Discord OAuth exchange secret |
| GOOGLE_CLIENT_ID | empty | Enables Google identity prefill/verification and verified-email relay recovery |
| TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET | empty | Helix app credentials for permanent user ID → current login; missing credentials retain anonymous IRC and one-time missing-handle alerts |
| GA_ID | empty | Enables GA4 on public home/privacy/manage pages |

With `DONATE_URL` set, everyone runs all channels free and pages show a donate link. Without it: first 5 streamers everything free, then 1 channel free and multiple channels on a paid plan. Empty payment links hide the corresponding upgrade choices; the first channel still runs. Configure billing before advertising paid service. Google is optional identity verification, not a stored account or an email-login system. Manual email input sends no verification email.

## Relay HTTP interface

Responses are HTML except simple health/Stripe text responses; there is no general JSON registration API.

| Method / path | Inputs and result |
|---|---|
| GET `/` | Signup form |
| POST `/register` | URL-encoded name, email, webhook, and `tiktok` and/or `twitch` (the older `platform` + `channel` pair is still accepted); optional `google` credential and `via=discord` or `via=webhook`. 303 to manage on success; 400 validation error; 503 new-registration capacity reached |
| GET `/discord/callback` | One-use state and code, valid for 900 seconds; exchanges Discord authorization for the selected channel's webhook; 400 on failure |
| GET `/m/{token}` | Private manage page; 404 absent/deleted registration |
| POST `/m/{token}/save` | Same validation as registration, blank webhook retains current credential; replaces settings while preserving private token, plan, founder reservation, subscription and creation date; restarts relay and confirms to the new webhook |
| POST `/m/{token}/report` | Writes sanitized channel/status diagnostics to bounded `support.jsonl`; one report per account per minute; never credentials, tokens, email or chat |
| GET/POST `/recover` | Google sign-in page / verifies Google credential and redirects to a matching account or offers matching channel lists; unverified visitors never learn whether an email exists |
| POST `/m/{token}/test` | Posts a test for any plan; 404 inactive link. “Test sent” is not a delivery receipt |
| POST `/m/{token}/delete` | Deletes registration and in-memory stats, stops runner. Does not cancel Stripe billing |
| GET `/privacy` | Stored data, analytics and deletion policy |
| GET `/health` | 200, `ok` plus registration count; liveness only, not end-to-end delivery |
| GET `/admin` | Protected by configured `ADMIN_KEY` query credential; unset or wrong key returns 404 |
| POST `/stripe-webhook` | Signed JSON event, max 512 KiB; 400 bad signature, 200 processed/ignored event |

Every response carries `Content-Security-Policy` (external origins limited to Google sign-in, Google Fonts, Google Analytics and Discord as a form/OAuth target; `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` and `Strict-Transport-Security`; `Server` is `LiveChatXR` with no Python version. Form POSTs whose `Sec-Fetch-Site` is present and not `same-origin`/`none` return 403. `/register` and `/recover` allow 20 attempts per client per 10 minutes (client = last `X-Forwarded-For` entry, else the socket peer; loopback without that header is exempt for the deploy probe) and then return 429 with `Retry-After`. Request sockets time out after 30 seconds.

`ADMIN_KEY` is empty by default; then admin is disabled. Keep its private access link in approved secret storage, not browser screenshots, reports or shell arguments.

Registration constraints: name truncated to 60 characters, email lowercased and syntax-checked (local part 1–64, domain part 1–190, final suffix at least two letters), TikTok handle 2–24 letters/digits/underscore/dot, Twitch 3–25 letters/digits/underscore. Leading `@` and supported TikTok/Twitch profile URLs normalize on the **server only**. Discord webhook must be HTTPS on discord.com or discordapp.com (including ptb/canary), numeric ID 15–22 digits and token 40–100 word/hyphen characters, then pass a Discord lookup. Ordinary POST bodies over 8192 bytes return 413. Manage-path tokens must be 20–64 word/hyphen characters.

TikTok signup checks profile hydration even when offline. A missing/unavailable account is rejected; network failures or inconclusive profile responses allow signup with a warning. Twitch signup checks syntax only. Reusing exactly the same webhook preserves token, plan, subscription and creation time, updating identity/channel fields. The first `FREE_SLOTS` registrations are `founder`; later registrations are `free` and immediately run only the first saved channel (signup order: TikTok, Twitch, YouTube). All extra channels remain saved. A signed `checkout.session.completed` with `payment_status=paid` and matching `client_reference_id` changes a non-founder to `paid` and restarts all saved channels. `customer.subscription.deleted` returns the matching paid entry to `free` and restarts its first channel; founders are never downgraded. The server does not handle every Stripe billing event, such as payment failures.

On first load, existing free/missing-plan records ordered by `created` (token breaks ties) fill founder slots. Existing pending records become free, not founders. Before the first migration write, `registrations.json` is copied byte-for-byte to `registrations.pre-founder.json`, never overwritten on retry. `founders.json` stores lifetime slot reservations and marks migration complete, so restarts never promote newer free users and deleting a founder does not reopen a slot. Back up and restore it alongside registrations; it contains private registration tokens. Reservation is saved before a new founder registration, so an interrupted write can consume a slot but never award more than the limit. Free and founder chat batches retain discovery tags; paid batches have none.

Signatures use HMAC-SHA256 with a 300-second timestamp tolerance. This is not a durable event-ID replay ledger. Persistent files are `registrations.json`, `stats.json` (saved on changes at 30-second intervals), rotating `relay.log`/`.1`, and backup timestamp files `last_backup`, `last_offsite`. `relay.log` is also the Discord post timeline: one `N lines queued` line per collection window, one `posted N lines` or `post failed (code)` line per webhook post, and `N lines collapsed` when backlog is summarized. Counts only; chat text is never logged. Tokens, webhooks and subscription IDs in registration data are private. Admin shows counts, status, failures, recent logs and backup age, refreshing in 30 seconds; ages beyond 36 hours warn.

Registrations also hold an `identities` entry per resolved platform (permanent ID, current handle, optional TikTok `sec_uid`, and the handle already alerted). Startup enriches saved registrations in background threads; failed lookups are logged by exception type and never prevent startup or relay connections. Twitch IRC `ROOMSTATE` supplies an anonymous room ID as a fallback; a reclaimed login with a different room ID is never relayed. YouTube connects via `/channel/UC.../live`. TikTok remains handle-based. Three consecutive not-found checks on a known ID send one update-link alert per handle change. `support.jsonl` rotates at approximately 1 MB into `.1`; it contains normalized error categories, last success timestamps, plan, channel names, a source-content version hash and uptime. There is no configured mail sender, ops intake or Telegram alert path; support is local-log-only until one exists.

## Command interface

- `app/livechat_xr.py` without arguments launches the Windows tray app. Exact argument `--check` imports bundled TikTok events and exits without starting UI/chat.
- `app/chat.py` without arguments uses the local config for a headless runner. Its alternate three-argument form accepts platform, channel and webhook, but puts the webhook in process arguments; prefer protected config instead. This is not the hosted relay.
- `server/server.py` starts the HTTP relay, migrates legacy plans and restores every registration with its plan's channel limit, staggering startup by 3 seconds.

See [development](development.md) for exercised local commands and [operations](operations.md) for the hosted service.

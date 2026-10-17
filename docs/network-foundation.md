# Private Pop1 discovery foundation

This is the T8–T10/T13/T16 foundation, not Stage 1 activation. Hosted chat and its billing remain unchanged. No public list, `/optin`, squad, follows, paid priority or TikTok presence is enabled. Source quota/terms gates still apply before activation; mocked collector tests are not a live quota measurement.

## Runtime and integration contract

`server.main()` constructs `network.Network(DATA, normalise_channel)` on the existing asyncio loop. `start()` validates configuration and initializes `DATA/listings.db`; `stop()` cancels and joins its asyncio tasks. SIGTERM and normal loop shutdown invoke `stop()`. A running urllib operation cannot be forcibly interrupted: it has a 10-second timeout and the stop event prevents another page request. All collector, identity, SQLite and Discord I/O initiated on the loop is offloaded with `asyncio.to_thread`. At most one poll per source is outstanding; failures back off without refreshing last-success.

Required configuration names (values must never appear in logs):

- `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_CLIENT_ID`
- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `YOUTUBE_API_KEY`
- `POP1_LIVE_GUILD_ID`, `POP1_LIVE_CHANNEL_ID`, `POP1_LIVE_FOUNDER_ID`

An absent name keeps discovery off and logs names only; hosted chat still starts. Invalid public-key/storage initialization also fails discovery closed. Use the existing application/client ID and private test guild; no global commands are registered. The only supported command is `/network-status`. Unknown/staged commands receive an explicit unavailable response, never a fabricated opt-in confirmation. OAuth settings are not changed.

`POST /discord/interactions` accepts at most 64 KiB and verifies Ed25519 over timestamp + raw bytes before parsing JSON; timestamps older/newer than five minutes are rejected. PING returns PONG. Commands require the configured guild or founder identity in DMs, plus the configured application ID. Supported commands ACK ephemerally before async work, then send an ephemeral follow-up. A bounded five-minute interaction-ID cache prevents duplicate follow-ups in one process; this is not durable command idempotency. Future state-changing commands must additionally deduplicate transactionally.

Twitch uses the existing `chat._twitch_access` token cache, resolves the Population: ONE category and exhausts Helix pages every five minutes. YouTube uses one Pop1-only `search.list` query every fifteen minutes, paginates, then checks official `videos.list` live details, actual start/end and game text. Missing viewer count stays unknown and is not shown as a small stream. These are all-day intervals measured after each completed poll. Quota failures back off for at least an hour. API 429 responses respect retry delay; failed/partial traversals never replace presence or mark the source successful. No scraping fallback, census scheduler or census database is copied.

Collector traversal is adapted from the located `vr-census-tool/census/twitch.py:helix` and `youtube.py:api`, reduced to Pop1 and stdlib transport with 10-second timeouts. Network identity resolution reuses `normalise_channel` and Twitch's `chat.resolve_identity`; YouTube uses the official channels API instead of the shared 15-second scrape. Identity resolution alone is not ownership verification.

## Storage and follow-on seam

Each operation opens/closes its own SQLite connection; writes are transactional, with WAL and foreign keys enabled. Initialization is repeatable `CREATE TABLE IF NOT EXISTS`, not a production migration.

- `listings`: stable integer `id`, `discord_id`, `opted_in`, `review_status` (`pending`, `reviewed`, `removed`), `dm_closed`.
- `channels`: stable integer `id`, `listing_id`, `platform`, permanent `platform_id`, current `handle`; unique `(platform, platform_id)`.
- `presence`: keyed by `channel_id`, stream ID/title/viewer count/game/last seen/live flag.

`Store.upsert_presence(platform, rows, now)` is for complete polls only. New discoveries start pending and not opted in. It updates presence/handles, never consent, review, ownership or DM flags. A channel-ID-only YouTube poll preserves an already resolved handle. `Store.set_listing(id, **fields)` is an internal trusted state API, not an authorization boundary: the surfaces lane must verify ownership/reviewer authorization before calling it or grouping channels under a listing. `listing`, `channels` and `discovery` return detached dictionaries. `discovery(now)` excludes unconsented, pending, removed, unknown/over-ten-viewer and older-than-thirty-minute presence. It returns channel rows; the surfaces lane owns grouping/rendering and cross-platform eligibility policy.

`Network.snapshot()` returns detached `enabled`, missing-name list, sanitized startup error and per-source `{last_success, error, success_streak}`. No-success starts at zero; failure resets the streak but preserves last-success. T11 owns incident timing/window/recovery and presentation, using this same runtime state. `Store.dm_closed_count()` provides persisted closed-DM state to the admin caller (offload it when called from the loop).

`Network.discord` retries 429 at most three times using nonblocking waits. `send_dm(listing_id, text)` prefixes Pop1 Live, persists Discord 50007 and suppresses subsequent automatic attempts for that listing, including after restart. Other failures propagate to the owning task's sanitized handler. `message(text)` escapes markdown and neutralizes mentions, always supplies `allowed_mentions`. Its optional `viewer_role` is reserved for a later explicit, configured viewer-role send; no active command/list/ping calls it. No arbitrary user/role/everyone mentions are permitted.

## Requirement → runnable proof

Run with Python 3.12 and PyNaCl 1.6.0 (server/test dependency only):

`python -m unittest tests.test_network tests.test_discord_delivery -v`

| Requirement | Runnable check |
|---|---|
| T8 lifecycle, catch/retry, missing every required name | `Runtime.test_start_stop_and_retry_do_not_refresh_success`, `test_missing_each_config_is_off` |
| T8 real image COPY/import layout, production main startup/shutdown, network-only version change | `Packaging.test_docker_copy_layout_import_and_network_version` |
| Collector pages, permanent IDs, repeated cursor, ended/unrelated/unknown-viewer YouTube exclusion | `Collectors.test_twitch_pages_permanent_ids_and_repeated_cursor`, `test_youtube_pages_live_game_and_unknown_audience` |
| Timeout (including token refresh), quota and 429, failure freshness and recovery | `Collectors.test_http_timeout_including_discord_and_token_refresh`, `test_rate_limit_headers_and_bad_response_do_not_become_success`, `Runtime.test_real_poll_success_failure_recovery_and_quota_backoff` |
| Official identity reuse, no YouTube scrape | `Runtime.test_identity_uses_normalizer_and_official_youtube_not_scrape` |
| T9 real local HTTP + generated Ed25519, signature/body failures | `Interactions.test_real_signature_ping_and_invalid_payloads` |
| T9 <3s ACK, >3s command then real fake-Discord HTTP follow-up, private scope | `Interactions.test_slow_command_ack_then_followup_and_scope` |
| Private guild registration, no staged commands | `Runtime.test_registration_is_private_and_idempotent` |
| T10 HTTP-thread removal/collector transaction overlap in both writer orders, dedupe/rename | `Storage.test_http_removal_contends_with_collector_transaction`, `test_removal_and_upsert_both_orders_and_rename` |
| Consent/review/size/freshness filtering | `Storage.test_visibility_requires_consent_review_size_freshness` |
| T13 E3 actual 5s production-scheduled blocking collector; independent thread emission/post clocks, every line <0.5s while blocked | `Delivery.test_hosted_blocking_collector_keeps_each_line_under_half_second` |
| T13 E4 raising collector and every missing config: hosted mixed-platform 10/10 within 2s | `Delivery.test_hosted_crashing_collector_delivers_ten_within_two_seconds`, `test_hosted_missing_each_secret_delivers_ten_within_two_seconds` |
| T16 markdown/mentions, explicit role-only payload, 429 wait, persistent 50007/no retry | `Runtime.test_discord_mentions_retry_and_closed_dm` |

The existing hosted mixed-burst fixture is shared by the isolation cases and retains its two-post baseline. A negative probe replacing collector offload with an inline call fails the blocking case (TimeoutError); it does not falsely pass on a frozen-loop clock. No live accounts or production state are used.

CI adds these network/delivery tests to the existing Ubuntu relay lane and pins PyNaCl in both test environments. The existing Windows full suite still builds/tests the OpenXR DLL and packaging; deploy tests and `server/deploy.py` are unchanged. This Python repository has no `npm run verify` script; unittest/build results are the relevant gates.

## Explicitly deferred

- T11/E1/E5: timed incidents, recovery notifications, admin presentation (follow-on core card).
- T12/E2 and T17: online snapshot/restore, deployment/architecture reconciliation (follow-on core card).
- Surfaces: `/optin` ownership auto-check, list renderer/poster, immediate removal edits, manage/profile UX, review flow and demand taps. The foundation does not claim these exist.
- T14 owner-comment hook: independent lane; app/chat.py and its tests are untouched here.
- O6 TikTok presence, squad/follow/public ping features and T15/payment: deferred, not covered or activated.

Rollback: disable discovery configuration and restart only through the existing reviewed-main controller. Preserve additive listings data and previous image; chat keeps its existing path. This PR does not deploy, install backup scripts, provision credentials or select a public guild. A draft PR/test pass is built, not merged, timer-deployed or publicly launched.

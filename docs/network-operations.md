# Discovery core: T11 / T12 / T17 and strict DR8

Repository implementation and isolated tests only. This is the handoff to existing
surfaces lane C and operations lane E, after canonical review/merge. No production
migration, backup execution/install, quota probe, activation or terms clearance.
[Architecture](architecture.md#private-discovery-d1-one-process-and-one-volume) and
[operations](operations.md#discovery-backup-and-restore) describe the active design.

## Exact Store API and provenance

Every method is synchronous: use `Network.offload` when calling from the relay
loop. HTTP handlers already run on worker threads. Connections close per operation;
mutations take `BEGIN IMMEDIATE`, foreign keys are enabled, and the in-process lock
serializes peak changes with removal. Method names are **not authorization**.
No public route currently exposes these mutators; C must enforce authenticated
listing ownership, fresh channel ownership proof and separate reviewer authority.

- `Store(path)`, `initialize()`: schema `PRAGMA user_version=1`. Accept a new DB,
  the exact foundation version-0 schema, version-1 full schema, or sanitized
  version-1 restore. Reject unknown versions, ambiguous `presence` + `live_streams`,
  unexpected tables/columns/triggers/views. Upgrade is transactional: add
  `removed_at`, observation/verification fields, rename `presence` to `live_streams`,
  create `supplied_channels`, expire data, check integrity/FKs. Failure rolls back
  DDL and version; repetition is safe. Existing `channels.verified=0`, no provenance
  backfill, no inference from `discord_id`/review/current associations.
- `record_supplied_channel(listing_id, platform, handle)`: internal trusted flow
  **only after authenticated proof that this listing owner controls this exact
  streamer-supplied handle**. Canonical lowercase Twitch `[a-z0-9_]{1,25}` or
  YouTube `@[\w.-]{3,30}`, validated platform. Canonical `(platform,handle)` is
  unique across owners, duplicate same-owner calls are idempotent, conflicts fail
  transactionally. Never pass an API-derived replacement handle or platform ID.
  Collectors never call this method or change its rows.
- `bind_verified_channel(listing_id, platform, handle, platform_id)`: C calls only
  after **fresh** ownership proof resolves that supplied handle to this platform
  identity. Requires supplied provenance, a nonremoved listing, and an observed
  channel. A channel owned by another supplied/consenting/reviewed listing cannot
  be stolen. Collector-only pending shells may be rebound explicitly. This sets
  ephemeral `verified=1`; stored consent/review alone is insufficient. Restored
  listings have no platform identity, and a matching/reassigned handle does not
  implicitly bind them. Never use raw SQL to bypass this boundary.
- `upsert_presence(platform, rows, now)`: signature retained for callers; storage
  is `live_streams`. Complete successful poll only. New identities create pending,
  unconsented, unverified shells; existing consent/review/removal/provenance never
  changes. Empty complete poll removes that source's live rows. Failed/partial
  requests never call this method. Collector identities expire after 24 hours
  without observation, independently of listing history.
- `set_listing(listing_id, *, now=None, **fields)`: trusted fields are `opted_in`,
  `review_status`, `discord_id`, `dm_closed`. Removal/false consent atomically
  clears active consent, all platform rows/bindings and memory peaks, recording
  `removed_at` once. Repeated removal never extends the deadline. Explicit
  authenticated re-opt-in before seven days clears removal but forces pending
  review, even if the caller requests reviewed. An expired re-opt-in is rejected
  and the expired record is deleted. Normal review remains separately authorized.
- `expire(now=None)`: deletes overdue removed listings/dependents/supplied handles,
  old platform rows and old peaks. Timestamp-less legacy removals purge rather
  than receiving a guessed date. Called at startup and from the existing runtime
  maintenance task; snapshot/restore apply removal expiry independently.
- `listing(listing_id)`, `channels()`: detached internal records, **not public
  eligibility APIs**. Do not display their collector-only/unbound entries.
- `live_channels(now=None)`: verified/consenting/reviewed current channels younger
  than 30 minutes, with independent raw viewer counts (including `None`). Used
  by C for per-platform labels/links. No TikTok presence.
- `discovery(now=None)`: one primary row per eligible listing. First live platform
  in Twitch → YouTube order is selected **before** count filtering. Primary must
  be Pop1 with a known 0–10 raw count. Unknown never becomes zero, never uses a
  known secondary or a peak. No cross-platform sum/max. C owns the complete list
  renderer, freshness copy and downstream matching/notification eligibility.
- `peak(platform, platform_id, now=None)`: ephemeral process memory only; highest
  observed raw count for that channel's latest observed stream, timestamped at
  observation, expires within 24 hours. New stream resets the peak. Removal clears
  it; restart/restore loses it. `None` means no record: future offline matching may
  use the documented 0–2 band, **never** replace an unknown current-live count.
- `dm_closed_count()`: persisted closed-DM count for the admin panel, not ownership.

## Runtime / incident contract

`Network.snapshot()` returns detached `enabled`, `missing` (names), `error`
(sanitized startup error), and source keys `twitch` / `youtube`, each containing
`last_success`, `error`, `success_streak`, `delayed`, `incident_since` (epoch or
`None`). C consumes this exact state; do not create another incident timer.
`record_poll(platform, *, error=None, now=None)` is the poll transition.
`check_incidents(now=None)` attempts one founder notification on entry/recovery.
`maintain()` performs off-loop expiry and incident checks in the existing runtime.
Pacific 18:00 inclusive / 23:00 exclusive, 15-minute stale entry, two consecutive
successful polls for recovery; failures reset the streak. Source state is in memory
and restarts begin a new monitoring epoch. Founder delivery failure is logged,
not retried on every maintenance tick. Discord transport retains bounded 429 retry.
Pinned `tzdata` supplies IANA data on Windows; timezone logic is stdlib `zoneinfo`.

Required names are unchanged: `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`,
`DISCORD_CLIENT_ID`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `YOUTUBE_API_KEY`,
`POP1_LIVE_GUILD_ID`, `POP1_LIVE_CHANNEL_ID`, `POP1_LIVE_FOUNDER_ID`. No values in
panel/logs. Chat-health reporting is independent of network state.

## Sanitizer / restore seam for C and E

`backup.sanitize(db, now)` is the versioned allowlist seam. Full-schema inputs must
match `backup.DURABLE | backup.EPHEMERAL`; schema changes must update this and tests.
Final snapshot tables/columns:

- `listings`: `id`, `discord_id`, `opted_in`, `review_status`, `dm_closed`, `removed_at`.
- `supplied_channels`: `listing_id`, `platform`, `handle`.

Keep only listings with supplied provenance or durable consent/review/removal
state; default pending/unconsented collector shells are omitted. This selection
is **not** permission to infer provenance from listing flags. Empty Discord IDs
are not a deletion rule. Collector IDs/handles, stream titles/counts/game, peaks
and visit identifiers never enter the snapshot. VACUUM removes deleted payload
bytes; integrity and FK checks run before atomic publication and on actual archive
contents. There is no raw DB/WAL/SHM elsewhere in the accepted nightly archive.

The temporary online SQLite copy is not a deliverable. Unique unfinished copies
are cleaned on handled failure and always excluded from archives after a crash.
A fresh run removes any prior `listings.snapshot.db` before copying; failure cannot
validate the old file. Nightly shell has a process lock and explicit step exit
checks, not a Docker/gzip pipeline that hides a failed producer.

Commands are in [operations](operations.md#discovery-backup-and-restore). Restore
requires a stopped writer, expires removals **before serving**, and retains only
unbound listing state/supplied handles. Tests start the real `Network` on restored
data with fake collectors and prove renamed/reassigned handles cannot revive it.
Historic 30-day archives, offsite copies and the unchanged deploy controller's
paused raw rollback tar remain a production-retention limitation. Seven-day active
record deletion is not seven-day physical erasure from those archives. C/E must
not publish stronger promises without implementation and verification.

## Requirement → runnable proof

Reconstructed from the repository's reviewed codepath table, not the external
historical test artifact. Five critical paths: (1) gated lifecycle and authenticated
HTTP ACK/follow-up; (2) complete collector polling and consent-preserving writes;
(3) incidents/admin; (4) online sanitized backup and unbound restore; (5) hosted chat
isolation. `/optin` user UX and posted-list E2E are still C's work, not claimed here.

Run Python 3.12 with `PyNaCl==1.6.0`, `tzdata==2026.5`, and app requirements for the
full Windows suite:

```sh
python -m unittest tests.test_network tests.test_network_operations tests.test_network_backup tests.test_discord_delivery -v
python -m unittest discover -s tests -v
```

| Requirement | Runnable proof (class in named test module) |
|---|---|
| T8 lifecycle/missing settings/shutdown/image layout | `test_network.Runtime`, `Packaging` |
| T9 valid/tampered Ed25519, real HTTP ACK + slow follow-up | `test_network.Interactions` |
| T10 migration rollback/repeat/unknown provenance, conflict/removal/re-opt/expiry | `test_network_operations.Retention`; `test_network.Storage` actual writer contention both orders |
| T11 E1 separate sources, no initial poll, repeated stale ticks, failure resets two-success recovery | `test_network_operations.Incidents.test_independent_incidents_startup_repeats_and_two_good_polls` |
| T11 Pacific window and both DST-change dates | `Incidents.test_window_boundaries_dst_and_disabled` |
| T11 actual poll/maintenance state and notification transport/failure | `Incidents.test_runtime_maintenance_and_real_poll_use_same_state`, `test_founder_transport_and_notification_failure_do_not_repeat` |
| T11 E5 panel ordering, healthy/off/stale/closed DMs, chat state, auth/XSS via local HTTP | `test_network_operations.AdminHTTP` |
| T12 E2 concurrent writer and actual snapshot payload absence/allowlist | `test_network_backup.Snapshots.test_concurrent_online_snapshot_no_platform_bytes_or_shells` |
| T12 actual archive members, JSON continuity, corrupt snapshot, missing DB, failed sanitizer/stale cleanup | `Snapshots.test_archive_contents_integrity_chat_only_and_corruption`, `test_failure_discards_stale_snapshot_and_temp_corruption_rejected` |
| T12 actual restored runtime, just-written opt-out, rename/reassignment, fresh rebind and restore expiry | `Snapshots.test_restore_real_runtime_unbound_renamed_handle_and_expiry` |
| T12 Linux running/stopped Docker branches, real helper/tar/gzip/checks, success/failure stamps | `test_network_backup.NightlyShell` (Docker transport fake; no live host) |
| DR8 unknown-primary/known-secondary/no-live, raw counts and peak fallback | `Retention.test_unknown_primary_is_not_secondary_count_or_peak_fallback`, `test_live_end_24h_expiry_peak_unknown_and_restart` |
| T13 E3 actual 5-second blocked collector, independently timed per-line delivery <0.5s while blocked | `test_discord_delivery.Delivery.test_hosted_blocking_collector_keeps_each_line_under_half_second` |
| T13 E4 crashing collector / every missing setting, 10/10 lines within 2s | `Delivery.test_hosted_crashing_collector_delivers_ten_within_two_seconds`, `test_hosted_missing_each_secret_delivers_ten_within_two_seconds` |
| T16 mentions, bounded 429, persisted 50007 suppression | `test_network.Runtime.test_discord_mentions_retry_and_closed_dm` |
| T17 active D1 docs and API/schema/retention ledger | this ledger + architecture/operations/design diff review; runnable storage/archive/runtime checks above validate the stated contracts |

Nine explicit edge families: missing settings; bad signature/replay/private scope;
unknown/ended/stale counts; quota/429/timeouts; opt-out versus collector in both
orders; DST/startup/recovery gaps; closed/failed DMs and unsafe external text;
corrupt/interrupted/chat-only backup; restored reassigned handles/overdue removals.

E3/E4 assertions are unchanged, not relaxed. Linux CI runs the new shell/snapshot
checks in the existing relay job; Windows CI runs the full suite and native build.
No `npm run verify` exists in this Python repository. O6, F1 quota evidence, public
terms acceptance, staged squad/pings/payments, E6 unique visitors and UI ownership
flows are **not** covered by these fixtures. `/go` remains C's lane; raw click totals
cannot pass E6, and no visitor identifiers or new scheduler are introduced here.

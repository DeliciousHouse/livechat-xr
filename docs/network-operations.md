# Private discovery core and Discord surfaces (strict DR8)

Repository implementation and isolated tests only. This is the handoff to existing
web surfaces lane C2 and operations lane E, after canonical review/merge. No production
migration, backup execution/install, quota probe, activation or terms clearance.
[Architecture](architecture.md#private-discovery-d1-one-process-and-one-volume) and
[operations](operations.md#discovery-backup-and-restore) describe the active design.

## Exact Store API and provenance

Every method is synchronous: use `Network.offload` when calling from the relay
loop. HTTP handlers already run on worker threads. Connections close per operation;
mutations take `BEGIN IMMEDIATE`, foreign keys are enabled, and the in-process lock
serializes peak changes with removal. Method names are **not authorization**.
No public web route exposes these mutators. Private signed Discord interactions
enforce listing ownership, fresh channel proof and separate founder authority.

- `Store(path)`, `initialize()`: schema `PRAGMA user_version=2`. Accept a new DB,
  the exact foundation version-0 schema, version-1/2 full schema, or sanitized
  version-1/2 restore. Reject unknown versions, ambiguous `presence` + `live_streams`,
  unexpected tables/columns/triggers/views. Upgrade is transactional: add
  `removed_at`, observation/verification fields, rename `presence` to `live_streams`,
  create `supplied_channels`, expire data, check integrity/FKs. Failure rolls back
  DDL and version; repetition is safe. Existing `channels.verified=0`, no provenance
  backfill, no inference from `discord_id`/review/current associations. Version 1→2
  transactionally adds only `ign TEXT NOT NULL DEFAULT ''`,
  `timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles'`, `alerts_enabled INTEGER
  NOT NULL DEFAULT 0`, `would_pay INTEGER NOT NULL DEFAULT 0` (boolean checks).
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

- `listings`: `id`, `discord_id`, `opted_in`, `review_status`, `dm_closed`, `removed_at`,
  `ign`, `timezone`, `alerts_enabled`, `would_pay`.
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
terms acceptance, staged squad/pings/payments, E6 unique visitors and live Discord /
Quest acceptance are **not** covered by these fixtures. Private signed ownership
and `/go` delivery now have the additional isolated proof below; raw click totals
cannot pass E6, and no visitor identifiers or new scheduler are introduced here.

## C1 shared surface API (T18–T21 / T24–T25)

Outcome owner: C `t_f8b3e2b6`; serial successor: C2 `t_b1e354a7`. C1 implementation
completion is not merge acceptance. C2 must wait for C1's sole canonical review
completion and verify its merge SHA on main before editing these hotspots.

- `Store.owner_listing(discord_id, *, create=False)` is an internal, serialized
  owner lookup/create seam. The caller must authenticate the ID. Ambiguous legacy
  duplicate owners fail closed; do not merge or take over somebody else's listing.
  New owner shells start with DMs closed until successful welcome delivery.
- `Store.profile(listing_id)` returns detached private listing/profile data or
  `None`; it is **not** an unauthenticated public read API.
- `Store.update_profile(listing_id, *, ign=None, timezone=None,
  alerts_enabled=None, would_pay=None)` validates explicit non-None fields only.
  IGN: 1–64 printable characters, no controls or mention delimiters. Timezone:
  stdlib `ZoneInfo` IANA validation. Preferences: actual booleans, not string
  coercion. No platform-derived IGN is written. Profile/demand data is deleted
  with the listing after seven days, not immediately on opt-out.
- `Store.demand()` returns `{"total": N, "outside_cohort": None}`. One flag per
  authenticated streamer, including removed listings until their deletion. C2's
  admin must label outside-five demand **unavailable**, never infer membership
  from a handle, Discord ID or the total. This is not payment/checkout consent.
- `Store.matching_count(listing_id, now=None)` returns `(raw_count, offline)`.
  Current live primary Twitch→YouTube is selected before checking for unknown.
  Offline uses only that primary channel's latest-stream memory peak; no secondary
  fallback. Restart/24-hour expiry returns `(None, True)`. `similar_size(count,
  other, offline=False)` implements mutual half-to-double ranges, with 0–2 covering
  0–4; unknown current live never matches. Self matches are excluded by listing ID.
- `await Network.remove_listing(listing_id, *, reject=False)` is shared by private
  opt-out, founder remove/reject and future authenticated web untick. Authorize
  **before** calling. It immediately removes consent/platform data/peaks, cancels
  that owner's challenge, demand buttons and alert edges, invalidates redirects,
  then edits the one list and every tracked naming ping under the surface lock at
  any hour. Naming pings are replaced with a non-naming tombstone, not left stale.
  `True` means remote edit attempts succeeded; `False` means local removal stands
  but transport failed. Do not claim remote success in the latter case. Existing
  maintenance makes at most four edit reconciliation attempts; discovery of an
  old list after a failed history lookup gets three attempts. No endless retry.
  Closed DMs cannot recall already-delivered device notifications.
- `Network.resolve_go(opaque_id)` is synchronous for threaded HTTP handlers.
  Returns an allowlisted platform HTTPS destination or `None`. Renderer-generated
  random 128-bit mappings hold listing/platform/stream/destination, expiry and
  publication state **in memory only**. Expiry is at most the 30-minute live-data
  visibility window (never beyond 24 hours); removal/restart invalidates links.
  Resolution rechecks current verified consent/stream and exact destination.
  `/go/<id>` returns immediate 302/404, no-store/no-referrer, zero response body,
  no cookie, analytics, redirect access-log trail, or management token. Queries,
  credentials, fragments and arbitrary hosts/paths are not accepted destinations.

### Private command/proof lifecycle

Only the existing configured private guild receives `network-status`, `live`,
`optin`, `profile`, `optout`, `review`, `remove`. There is no global registration,
public activation, `/follow`, `/squad`, role ping or name-sharing implementation.
`staged_copy` / `staged_ping` are clearly labelled private copy fixtures, not enabled features.
Desired identity is **Pop1 Live**, LiveChat XR violet avatar,
**by LiveChat XR · livechat.deliciouswines.org**. D owns vendor identity changes.

Commands/buttons/autocomplete share Ed25519, timestamp, application, private-scope
and replay validation. Guild sender comes only from the verified member; only the
configured founder can review/remove another listing, regardless of roles. The
welcome/profile demand button is random and bound to that exact owner/listing;
non-founder DMs can consume only an owner-bound button. Autocomplete returns type8
immediately (`los` → Los Angeles), not a deferred message. Other replies are
ephemeral with previews suppressed and `allowed_mentions.parse=[]`.

`/optin` creates a random single-use, owner/platform/canonical-handle/resolved-ID
challenge. It is memory-only with a monotonic 15-minute deadline. One-minute checks
use Twitch official title/bio or YouTube official channel description / an already
observed live video's title (`channels`/`videos`, never `search.list`). The same
original ephemeral reply is PATCHed throughout; there is no Check button. Identity
is re-resolved before and after reading proof. Only success invokes supplied-handle
provenance and verified binding, in one SQLite transaction; conflicts roll back
consent/provenance too. Offline proof observes an identity only, never creates live
counts/status. Restored bindings require new proof. Replacement, removal and stop
cancel the challenge, including a thread queued behind SQLite's removal lock.

Proof does not approve a listing: new/minor listings remain pending for a separate
founder decision. Welcome delivery must actually succeed to clear `dm_closed`;
retry `/optin` for an already verified binding retries the welcome, not a fake
recovery. Alerts require reviewed active consent, verified identity, an explicit
profile/notification preference and an open successful welcome. They are free,
all-hours, once per observed sender stream/recipient (24-hour process-memory edge
dedupe); not a scheduler, quiet-hours system or daily-cap feature.

The one list is reused from the bot's own recent dedicated-channel history after
restart (bounded 1,000 messages; a lookup failure never authorizes a duplicate).
It is edited every five minutes in 18:00≤Pacific<23:00, outside-window copy after
that, and paused copy on orderly stop. `/live` always uses the same renderer.
Counts/Watch on labels are per enabled platform; no TikTok or blended number. Source
timestamps are actual polls, not message edits. External fields alone are escaped;
authored markdown stays intact. Complete-row overflow, footer and UTF-16 size stay
within Discord's 2,000-character limit.

### E6 is UNAVAILABLE (not a click proxy)

The actual redirect does **not** measure unique visitors or retain a click counter.
C2's admin handoff must say **E6 visitor measurement unavailable — no threshold
passed**. Private/unposted links work for route testing but cannot pass a campaign.

Future proposal only, **not implemented or enabled**: first-party opt-in establishes
a random 128-bit browser ID for one 14-day campaign. A campaign-keyed HMAC dedupe
set and per-streamer contribution live in bounded memory only, no IP/UA,
fingerprinting, logs or backups. Repeat visits across platforms/streamers count
one visitor globally; contribution spread needs ≥3 distinct listed streamers.
Only actually posted IDs qualify; private/test/founder fixtures never do. Withdrawal
or campaign expiry clears identifiers. Restart/capacity eviction makes a campaign
incomplete/unavailable, not a fresh total. Cookie-consent/legal reconciliation and
a real posting campaign belong to Q/existing stages. Before enabling measurement,
accept this contract and add executable repeat/test-link/expiry checks. None of
those future tracking mechanisms are represented as built by C1.

### C1 requirement → runnable proof

Run Python 3.12 `python -m unittest tests.test_network_surfaces -v` plus the existing
network/operations/backup/hosted-chat suites and full unittest discovery. Linux CI
includes this exact module. Every Discord/API response is a **private fixture**;
the local HTTP handler, signing/verification, commands, SQLite and redirect are real.

| Requirement | `test_network_surfaces` runnable proof |
|---|---|
| T18 DR8 raw rows/footer, overflow, empty/paused/outside/delayed, source warning | `Surfaces.test_rows_states_staleness_and_overflow` |
| T19 exact DR2C/DR2E and staged DR3C, prefix/escaping | `Surfaces.test_authored_copy_and_external_fields_are_separate` |
| T20 same-original minute checks/success/blocked-DM | `Surfaces.test_proof_same_reply_minute_checks_success_closed_dm_recovery` |
| T20 wrong-ID/15-minute expiry/replacement/shutdown | `Surfaces.test_proof_expiry_wrong_identity_replacement_and_cancellation` |
| Official proof APIs and wrong-video owner | `Surfaces.test_official_proof_apis_no_search_and_exact_identity` |
| Proof conflict rollback/removal cannot restore | `Surfaces.test_failed_cross_owner_proof_is_atomic_and_cancelled_proof_cannot_restore` |
| Opt-out races an uncommitted first-owner proof | `Surfaces.test_optout_races_proof_before_first_owner_commit` |
| T21 02:00 list + all naming-ping edits, collector race, transport reconciliation | `Surfaces.test_post_schedule_removal_at_two_am_and_reconcile`; original `test_network.Storage` both writer orders |
| List reuse/author isolation/orderly pause | `Surfaces.test_restart_reuses_only_own_list_and_stop_pauses` |
| T24 successful welcome retry, bound demand button | `Surfaces.test_actual_welcome_retry_only_success_clears_closed_dm`; `SignedSurfaces.test_signed_autocomplete_buttons_replay_and_founder_ownership` |
| T24/T25 profile validation/detachment/demand and actual archive bytes/restore | `Profiles.test_profile_validation_detachment_and_archive_restore` |
| Schema1→2 rollback/repeat | `Profiles.test_v1_migration_transaction_and_repeat`; original v0/unknown-version/restore tests |
| T25 symmetric matching/current unknown/offline peak/restart/expiry/new stream | `Surfaces.test_primary_unknown_and_matching_peak_reset_expiry` |
| Free alerts, once-per-stream, consent/DM gates/no self | `Surfaces.test_alert_edges_consent_closed_dm_and_no_self` |
| Real signed HTTP opt-in→proof→profile→independent review→live→opt-out, forged IDs/scope/signature | `SignedSurfaces.test_real_signed_http_optin_profile_review_live_optout_and_forgery` |
| Signed autocomplete/buttons/replay/cross-owner/founder | `SignedSurfaces.test_signed_autocomplete_buttons_replay_and_founder_ownership`; original `test_network.Interactions` |
| Actual HTTP302/404/no-cookie/no analytics/open redirect/repeat/unposted/expired/restarted mappings | `SignedSurfaces.test_actual_go_http_noncollection_expiry_restart_removal_and_open_redirects` |

Physical Quest settings/notification behavior and real Discord rendering/delivery
are **unverified**, not inferred from desktop or transport fixtures. C2 owns the
separate web checklist/OAuth and truthful policy/audit assets. No F1/O6, moderation,
quota, live-posting, deployment, historic backup migration or terms clearance.
Rollback is a normal reviewed revert/config-off preserving chat; never restore
expired/deleted platform data or downgrade a live schema2 DB using an old writer.

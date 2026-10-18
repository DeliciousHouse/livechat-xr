# C2 private web surfaces and audit packet

Implementation for `t_b1e354a7`, following reviewed C1 merge `c032dbb`.
This is not C's merged-outcome acceptance, deployment, live OAuth setup, quota
approval, platform-sharing clearance or Quest acceptance. D/E/F1/O6 and public
activation holds remain. See [shared APIs](network-operations.md),
[accepted design](designs/vr-live-network.md) and [design tokens](../DESIGN.md).

## Private boundary

`server.NETWORK_WEB_PRIVATE` is false in normal execution. Only the isolated
HTTP test harness overrides it. No environment setting or public debug endpoint
activates onboarding. Existing associated owners may still remove their listing
when onboarding is off. Existing relay registration, webhook OAuth, billing,
status, test and stop controls are separate; relay signup never implies listing
consent, channel proof, review or go-live DM consent.

The existing Discord app needs an additional exact redirect URI:
`PUBLIC_URL + /network/discord/callback`. This is proposed configuration for D/Q,
not registered by this change. `webhook.incoming` remains the relay flow;
listing identity uses a distinct `identify` authorization-code exchange and
`users/@me`. Both external requests have 10-second timeouts. Identity is not
channel proof. Tokens remain local variables; random 256-bit state lasts at most
15 minutes, is bound to one registration, consumed before exchange and never
persisted. Withdrawal invalidates even a callback already exchanging a code.
No callback errors/logging echo codes, credentials or private manage links.

The existing private bearer manage link chooses the registration. Server-owned metadata stores
only `network_requested`, `network_listing` and `network_owner` beside existing
registration data. It is not copied to the listing snapshot. Both registration
and store locks protect conflict checks; an already-associated different owner
or registration cannot be merged/taken over. Save failure restores in-memory
metadata. The listing database never receives a relay webhook/email/manage
credential. New POST actions require an unpredictable CSRF value, exact Origin
and an allowlisted field set. Private pages/callbacks/admin are no-store and
no-referrer; listing pages contain no GA script or events.

`consent`, `link`, `profile`, `prove`, `optout` use `/m/<private-token>/network`.
There is no state-changing manage GET. `prove` explains the existing private
Discord `/optin` challenge; it does not start a second checker. Twitch/YouTube
only; TikTok presence remains excluded. `profile` calls `Store.update_profile`
with validated IGN and IANA zone; no new preference or checkout flow.
Removal calls the existing `Network.remove_listing` on the relay loop while the
HTTP worker waits at most 10 seconds. Timeout returns 202/pending without
cancelling reconciliation; remote failure says locally removed, not remotely
confirmed. A stopped loop permits local exclusion with an explicit 503 warning.

## Requirement → runnable evidence

Run with repository Python 3.12 and its existing requirements (including PyNaCl
and tzdata). No added runtime dependency or test framework.

| Requirement | Runnable check / implementation |
|---|---|
| T22 consent, five steps/status, next primary, defaults and field validation | `tests.test_network_web.Web.test_checklist_profile_proof_review_and_two_am_removal`; `test_public_policy_admin_and_accessible_markup`; `listing_section` |
| Separate identity code exchange, exact callback, finite timeouts, invalid identity rejection | `tests.test_network_web.Identify` |
| Unknown manage, CSRF, Origin, strict fields, missing/expired/replayed state, cross-registration owner conflict | `Web.test_private_gate_security_state_and_conflict` |
| In-flight callback withdrawal/re-consent race, disk-save rollback | `Web.test_callback_withdrawal_during_exchange_and_failed_save` |
| Upstream ownership conflict cannot grant profile/removal access | `Web.test_removed_or_reassigned_record_never_grants_another_owner` |
| Real local HTTP identity → profile → Ed25519-signed `/optin` → proof → separate founder review → listed → 02:00 web opt-out | `Web.test_checklist_profile_proof_review_and_two_am_removal` (external identity/source/Discord transports mocked, proof wait accelerated; actual server, loop, signature verification and SQLite) |
| Cross-owner/expired/replaced challenges and cancelled-proof races | Unchanged `tests.test_network_surfaces.Surfaces` proof/race cases and `SignedSurfaces` |
| Bounded wait, honest remote failure, immediate exclusion and live loop during blocked Discord HTTP | `Web.test_removal_pending_remote_failure_and_loop_responsiveness` |
| T23 one panel, chat first, network off then stale sources, escaped text, healthy summary, admin auth | `Web.test_incident_off_first_stale_rows_and_healthy_line`, unchanged `tests.test_network_operations.AdminHTTP` |
| T24 idempotent streamer demand and opt-in totals, no guessed outside-cohort count or E6 claim | `Web.test_public_policy_admin_and_accessible_markup`; existing `SignedSurfaces.test_signed_autocomplete_buttons_replay_and_founder_ownership` validates authenticated demand buttons |
| Homepage/privacy/terms HTTP 200, links, YouTube/Google references, removal and backup caveat | `Web.test_public_policy_admin_and_accessible_markup` |
| Shared data schema, sanitized snapshot/restore and fresh proof after restore | Unchanged `tests.test_network_backup`, `tests.test_network_operations.Retention`, `tests.test_network_surfaces.Profiles` |
| Hosted chat E3/E4 preserved, per-line timing not weakened | Unchanged `tests.test_discord_delivery.Delivery` blocked/crashed/missing-secret cases |

`python -m unittest discover -s tests -v` is the full suite. Build the layer first
on Windows to exercise its tests (`layer\build.cmd`). Linux CI exercises the
nightly-shell case. `npm run verify` does not exist in this Python repository;
it is not silently treated as a passing verification command.

## Policy claims → source and limits

| Public disclosure | Source / evidence |
|---|---|
| Relay registration and delivery counts; no retained chat text | `Handler.register`, `server.run`, `save`, `save_stats`; `tests.test_account.Account.test_relay_log_is_a_post_timeline_without_chat_text` and `tests.test_stream_stats` |
| Session summaries are distinct from census and registration deletion | `chat.Tally.row`, `server.run.save_session` (`sessions.jsonl`); `tests.test_stream_stats`; delete removes registration, not summaries |
| Public source identities, titles, raw counts and game; observation is not opt-in | `Network.collect_twitch`, `collect_youtube`, `Store.upsert_presence`, `discovery`; `tests.test_network.Collectors`, `Storage` |
| Official YouTube API discovery/proof versus separate chat reader | `Network.collect_youtube`, `resolve_identity`, `proof_visible`; `chat.youtube`; `tests.test_network_surfaces.Surfaces.test_official_proof_apis_no_search_and_exact_identity` |
| Immediate active platform-data purge on opt-out | `Store.set_listing`, `Network.remove_listing`; removal/concurrent-writer tests in `test_network`, `test_network_surfaces`, new web HTTP sequence |
| At most 24-hour platform-data/peak lifetime, 30-minute visibility | `Store._expire`, `peak`, `live_channels`; `Retention.test_live_end_24h_expiry_peak_unknown_and_restart`, `Storage.test_visibility_requires_consent_review_size_freshness` |
| Removed active listing deleted after seven days, unless re-opted | `Store._expire`, runtime maintenance/startup and snapshot/restore expiry; `Retention` and `Snapshots.test_restore_real_runtime_unbound_renamed_handle_and_expiry` |
| New sanitized snapshots contain no platform data/binding | `backup.sanitize`, `Snapshots` actual archive payload checks; historic archives are outside that guarantee |
| Historic backup erasure is not proved at seven days | `network-operations.md` sanitizer/restore seam and `operations.md`; prior policy's 90-day claim and documented 30-day/offsite/controller scopes are both disclosed, neither silently shortened |
| Deletion/contact route | Existing Stop and delete / `/optout` plus existing public GitHub issues route. No invented support address or promise to erase summaries/logs/archives by a deadline |
| Analytics | Existing public GA behavior remains; private listing/callback and `/go` use no analytics. No visitor measurement, cookies, IP/UA/fingerprinting or click-total stand-in added |

`/terms` is factual service/API-use information with YouTube Terms and Google
Privacy links, not new waivers, sharing permission or quota attestation. No
customer/legal commitment is created. Accurate platform-name text branding is
used rather than an invented approval badge.

## Reproducible private audit packet

`python -m tests.network_web_preview <output-dir>` exercises the actual local
HTTP harness, then renders existing Discord payload code through fake REST.
It exports homepage, policies, manage start/profile/proof and admin-incident
pages, strips all manage/CSRF/state values, disables static form submission and
labels each page PRIVATE FIXTURE. The Discord preview omits its verification
code and explicitly does not reproduce Discord chrome, timestamps or delivery.
`receipt.json` records the exact source SHA and dirty flag; only a clean committed
run is final evidence. External fonts may be fetched by the browser; no real
OAuth, platform or Discord requests are made by the fixture runtime.

Final images/HTTP receipt/verification live outside the worktree at
`C:/Users/bkam/AppData/Local/hermes/evidence/vr-live-c2-20261017` and are attached
to the implementation card. Capture every page at desktop 1440 and mobile 390;
`screenshots.json` records dimensions/overflow. Inspect actual captures with
vision, plus DOM checks for one primary, 44px controls, labelled Other zone,
Intl selection, no-JS fallback and keyboard focus. This packet is a labelled
private demo for Q to review, not an uploaded quota submission. If Google needs
a real live demonstration, that is outstanding; do not relabel these fixtures.

Visual review corrections: required IGN label/current-step semantics and mobile
admin tables in labelled keyboard-focusable horizontal-scroll regions. The
incident panel correctly follows global site navigation but precedes admin
content; it need not replace the site header. Policy density and inherited
header typography are retained to preserve the existing Read layout and full
retention distinctions. No new style/theme redesign or platform logo claim.

Live gaps remain separate: D/Q's OAuth redirect registration and actual sign-in;
reviewer's canonical merge and full-main suite; E's eventual deployed policy SHA
and public URLs; physical Discord/Quest behavior; F1/O6/terms/activation decisions.
No production commands or data operations are part of this packet.

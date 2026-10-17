# VR Live Stage 1 source gates — 2026-10-17

Research for card `t_c801d851`, against reviewed main
`176a0fc2ab3fc7f04a8c69bb7f92b93d89ca827c`. This is evidence, not launch
approval, a completed quota experiment, a TikTok presence contract, or legal
signoff. No runtime, existing collector, quota, project or public listing changed.

## Disposition

| Gate | Observed result | Activation consequence |
| --- | --- | --- |
| O6, TikTok game confirmation | TikTokLive **7.0.1** inspected; the single authorized owner-room probe was offline. No live room game field/value obtained. **Unresolved**, not evidence that TikTok lacks game metadata. | Exclude TikTok listings from public activation until a live sample and an explicit presence/game contract exist. Connectivity, seed membership and title matching are not game proof. |
| F1, YouTube quota | **Not passed.** No added polling: existing consumers plus documented default search allocation do not establish a safe reserve. The requested existing three-keyword collector needs at least 288 searches/day at 15-minute cadence; even a hypothetical single-query 96/day would overlap an active consumer. | Do not activate the proposed cadence. Use the safety-evidence branch of this research task, not an estimated or replayed 24-hour pass. |
| Platform terms | Authoritative sections reviewed; material display, cross-platform metric and retention conflicts remain. | No public Discord aggregate is cleared by this report; opt-in and free launch do not waive platform terms. |

DR1B remains free, fewest viewers first, no pin/payment/Stripe/T15. Squad-up and
other additions wait for the core decision. No host Discord selected, outreach
started, moderators contacted, or deployment performed.

## O6: library capability versus observed data

Installed research interpreter:
`C:/Users/bkam/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe`.
Metadata reports `TikTokLive==7.0.1`, `TikTokLiveProto==0.2.2`.

Source inspection found:

- `TikTokLiveClient.is_live()` delegates to `web.fetch_is_live()` after resolving
  the user. This is liveness, not game verification.
- `client/web/routes/fetch_room_info.py`, `FetchRoomInfoRoute.__call__`, returns
  the raw JSON `response.json().get("data", dict())` as `Dict[str, Any]`.
  `unique_id` selects `/room/info_by_user/`; `room_id` selects `/room/info/`.
  The library raises `AgeRestrictedError` on `prompts` or a data dictionary with
  at most one member. That heuristic is not proof of a user's age or game.
- The existing census uses `is_live` → `fetch_room_id_from_api` →
  `fetch_room_info(room_id=...)` because its prior by-user route returned empty
  data. Its `census/tiktok.py:80-84` matches **titles**, not game tags; its stored
  game labels are not O6 proof.
- Installed protobuf module `TikTokLiveProto.v3.webcast.model.viewer_picks`
  defines `ProfileExtra.game_interaction.external.game_name` and
  `game_tag_id` via `ProfileExtraGameInteractionExternal` (fields 1 and 4).
  These are **schema member chains**, not an observed room-JSON path, a
  documented room category contract, or evidence of a Population: ONE value.
  Other protobuf `game_tag` occurrences include interactive/rivals features;
  their names alone do not establish a broadcast's game.

Sanitized observed sample (owner-authorized relay streamer only, no websocket
or chat, no raw room payload persisted):

```json
{
  "library": "TikTokLive",
  "version": "7.0.1",
  "timestamp_utc": "2026-10-17T21:01:20.931084+00:00",
  "source_method": "TikTokLiveClient.is_live -> web.fetch_room_id_from_api -> web.fetch_room_info(room_id)",
  "outcome": "offline; inconclusive for game-tag capability",
  "observed_game_fields": {}
}
```

The method above is the planned chain; the offline `is_live()` result stopped
this probe before room lookup/metadata retrieval. **Exact observed Pop1 field
path/value: unavailable.** No negative schema conclusion is drawn. A later
live, authorized room sample must record only relevant game fields and verify
what their values mean before choosing the contract. No authentication bypass
or paid discovery was attempted.

Existing presence caveats (source at the baseline above, not new behavior):

- `app/chat.py:478-498,775-799`: connection transitions can call `on_connect`
  again after retry; a reconnect is not necessarily a new broadcast. A future
  contract needs broadcast identity/deduplication, not connection notifications.
- `app/chat.py:519-526,556-558`: viewer updates use integer `RoomUserSeqEvent.total`;
  cleanup sends `None`. Missing or disconnected counts are **unknown**, not 0.
  `server/server.py:468-472` does not pass `on_viewers` to the hosted relay.
- `app/chat.py:789-790` explicitly treats transport retry as not proof of stream
  end. Nor does a remembered connection prove current gameplay. Any future
  presence contract must timestamp source evidence, re-confirm after reconnect,
  and apply the plan's 15/30-minute stale rules without extending a timestamp
  just because a cached value or status string was read.

## F1: safety preflight, not a 24-hour measurement

Inspection preceded any YouTube request. **This research made zero YouTube Data
API calls**: search 0, videos 0, result pages 0, retries 0, failures 0, observed
quota errors 0. Observation start/end: **not started / not applicable**. There
is no pending measurement process or timer to wait for.

### Current documented costs

Official [quota calculator][YQ] and [search.list reference][YS], accessed
2026-10-17, both show last update **2026-10-15**:

> “The `search.list` and `videos.insert` methods have their own quota buckets.
> Each of these methods has a default daily limit of 100 per day. The quota
> cost is 1 per call.”

The other endpoints share a default **10,000 units/day**; `videos.list` costs
**1** there. Each additional result page is another request; invalid requests
cost at least one point. Retries must also be counted. Midnight Pacific is the
reset, not UTC midnight. The relevant reset instants on October 17, 18 and 19
are **07:00 UTC** (`America/Los_Angeles`, PDT). A future rolling 24-hour ledger
must be partitioned at each reset, not treated as one UTC date bucket.

Thus the plan's old “100 units/search; 9,600 of 10,000 units” is superseded as a
current cost claim. It was an estimate, never observed project usage.

### Existing workload evidence

Read-only inspection at `2026-10-17T21:06:49.511793+00:00`:

- Enabled existing global job `20ce2869c20a` (`vr-census-hourly`), cron
  `0 16-22 * * *`, executes `census_hourly.py` in the existing census tool.
  Per-job timezone is absent; no scheduler timezone inferred from that absence.
  The wrapper invokes `census.cli.main(['hourly'])` with all four configured
  games. No job was disabled or changed; this worker's own cron list being
  empty did not imply the global collector was absent.
- `census/games.json` has Pop1 queries `pop1`, `population one`, `pop one vr`.
  `census/youtube.py:153-192` runs `search.list(part=snippet,eventType=live,
  type=video,q=<each keyword>,maxResults=50)`, exhausts page tokens and verifies
  IDs with `videos.list(part=liveStreamingDetails,snippet)`, up to 50 IDs/batch.
  No explicit application retry loop; underlying transport attempts are not a
  complete ledger. API errors currently lead to HTML fallback; that is not an
  acceptable public-stage quota remedy (terms below).
- Existing SQLite coverage for the **previous** Pacific day,
  `[2026-10-16T07:00Z, 2026-10-17T07:00Z)`, contains **20 YouTube game runs,
  70 completed search pages and 52 completed detail batches**. These are
  historical, successful-request lower bounds, not this card's experiment or
  a Cloud quota metric; failed/in-flight/retried requests can be missing.
- Startup `YOUTUBE_API_KEY` is present, without inspecting its value. The census
  reads that variable. Exact Cloud project identity, effective allocation,
  other consumers and their reserve are **not established**. Q was consulted
  through the existing controller route (`ctx-75b6f54bcce44691`); no approved
  YouTube quota-metrics broker target/safe spare allocation was established.
  No credential or account was substituted.

### Calculated budget, explicitly not measured usage

At 15 minutes there are **96 polls/24h**. Reusing the existing three-keyword
collector means **at least 288 search requests**, before pages/retries, against
a documented default 100-search bucket. A hypothetical consolidated one-query,
one-page poll would use **96**, leaving only **4** for every other consumer,
page and retry. Adding that to the prior day's observed lower bound of 70
would be **166**, not a safe default-budget allocation. These calculations do
not establish the effective limit or actual exhaustion of this project.

**Safe-run decision:** do not consume another workload's unallocated reserve.
No genuine 24-hour measurement can be claimed. F1 stays unpassed on explicit
safety evidence, not on a fabricated quota error or an estimated pass.

Permitted free next step: Q/project owner establishes the effective Search
Queries allocation, usage and complete consumer reserve through an existing
authorized read-only consumer; then consolidate an approved query workload or
reduce approved frequency/scope, and measure the final candidate for a real
24-hour elapsed window with per-attempt accounting. A Boolean OR query is
supported by [search.list][YS], but its recall/pages must be measured rather
than assumed equivalent. Request a quota extension/compliance audit if needed;
no request submitted. **Do not create a second project for the same API client
to evade quota** ([YT §15][YT15], [YP III.D.1/3][YPD]). Even a legitimately
distinct client needs a separate policy/project assessment, not a sharding
workaround. Existing jobs, quotas and projects remain unchanged.

## Terms matrix

Sources accessed **2026-10-17**; quotations below are retrieved source text,
interpretations are marked. Twitch canonical page displays last modified
`12/04/2024`; YouTube Terms displays 2026-09-14 and Developer Policies
2026-10-15. Anchors identify authoritative sections, not a grant of permission.

| Planned use | Explicit authoritative rule / short excerpt | Interpretation and unresolved requirement |
| --- | --- | --- |
| Handles, game, live status, viewer counts and source links in Discord | [Twitch VII.B/C][TW7]: “update ... frequently” and “clear path to the source”; [YouTube III.F.2][YPF] requires source branding, including attribution per element for mixed-source results; [YT §10][YT10]. | Label each platform's values and link to the actual channel/live video. An overall platform footer or a generic emoji is not established compliant branding. Unknown counts remain unknown; do not replace them with 0. |
| Branding and tracked short links | [YouTube Branding, icon][YBI] permits the official icon for mixed-source, space-constrained attribution; [sizing/placement][YBS] requires clickable branding linked to YouTube content or the application's YouTube component. [Twitch VII.C][TW7] requires attribution and path to source. | Each link must preserve recognizable platform/destination and correct branding. Validate Discord's actual rendering/link destination; an opaque shortener alone does not establish compliance. Do not imply platform endorsement. No UI approval is asserted here. |
| Public cross-platform directory and opt-in subset | [Twitch VI.C][TW6]: data/insights may not be “made available to, or otherwise shared with ... third parties for any purpose without Twitch's prior written permission”; [VII.A][TW7] says not to commingle or modify/delete portions revealing incomplete datasets. [YouTube III.C.5][YPC] forbids modifying/replacing search content or presenting mixed-source results as YouTube search results. | Material Twitch permission/commingling issue for the proposed Discord aggregate, even free and opted-in. Separately labelled source records are safer than a merged platform metric but not a permission waiver. Confirm permitted display/filtering with authoritative clarification before activation; do not claim a complete platform census. |
| Per-listing cross-platform **maximum**, ≤10 filter and similar-size matching | [YouTube III.E.4.h][YPE]: must not “create new or derived data or metrics”; [III.E.2][YPE] restricts aggregation across unrelated owners. Twitch [VI.A/C][TW6] includes derived/combined insights. | A maximum is still a derived metric, not made permissible by avoiding a sum. Cross-source max ranking/matching and published comparative census metrics are not cleared; do not activate with YouTube-derived inputs absent an applicable permission/design disposition. Distinguish simple source-level display from statistical aggregation; neither opt-in nor same-person handle linking proves a recognized YouTube content-owner exception. |
| Possible new analytics exception | [YouTube III.L][YPL] applies only to audited developers with analytics use cases that explicitly applied for additional metrics/storage permission; [additional policy][YDER] is conditional on accepting an amendment. | No evidence this client has that permission/amendment. Its existence is not automatic clearance for max, custom game classification, rankings or long-lived census history. |
| Refresh, stale entries and in-place Discord edits | [Twitch VII.B][TW7] frequent accurate updates; [YouTube III.E.4.e/f][YPE] reasonable efforts for current data and most-updated data in user presentations; historical data must be accurate in time context. | Five-minute message edits cannot make a fifteen-minute source poll fresh. Preserve per-source evidence times, stale labels at 15–30 min and hiding >30 min. A single editable message reduces stale copies; ephemeral responses and downstream copies still need deletion/freshness treatment. These TTLs are product safeguards, not platform safe harbors. |
| Cache and stored census history | [Twitch Schedule 1.C][TWS]: do not store copies unless authorized, rights-controlled, or “cache ... for only a twenty-four hour time period without further sharing ... with third parties.” [YouTube III.E.4.b-d][YPE]: non-authorized statistics must not be stored >30 days; other limited non-authorized data must be refreshed/deleted by 30 days. | Current indefinite research storage is not permission to publish or retain historical metrics indefinitely. Design a source-specific retention/deletion policy including SQLite, exports, backups and Discord copies. Re-fetching today's count does not refresh an immutable old measurement. No existing history deleted by this docs-only task. |
| Opt-in, privacy, opt-out and revocation | [Twitch XIII.B][TW13], [Schedule 1.A/C][TWS], [VI.F][TW6]: notices, necessary consent, permissions and deletion on request/revocation. [YouTube III.A][YPA] requires terms/privacy disclosures; [III.D.2.c][YPD] requires easy revocation and prompt token revocation where OAuth applies; [III.E.4.g][YPE] requires deletion as soon as possible, within 7 calendar days, on user request/account deletion. | Existing listing opt-in is necessary product consent, not platform permission or YouTube OAuth authorization. Public API-key data is non-authorized data. `/optout`/manage-page removal ≤10 min must also trigger a deletion workflow, not merely hide a retained row; explain stored-data deletion does not delete YouTube's data. Revocation rules must cover linked identities, messages, exports and backups. OAuth-specific deadlines apply only if that flow is introduced; do not invent OAuth scopes. |
| Search quota, shared projects and fallback | [YT §15][YT15]: do not exceed/circumvent quotas. [YouTube III.D.1][YPD] requires exactly one project/client, exclusively for that client; [D.3][YPD] gives extension/audit path. [III.E.6][YPE] prohibits scraping YouTube applications; [D.7][YPD] prohibits undocumented APIs without express permission. Twitch [IV.C][TW4] prohibits circumventing rate limits; [XI.I][TW11] restricts undocumented materials. | The plan's separate-project escape hatch is not approved. Reconcile actual client/project consumers and allocation, stop owned work on quota errors, do not treat current HTML fallback or private web endpoints as a public-stage compliance solution. Existing relay/census behavior is outside this docs task and was not changed. |

**Terms disposition:** review performed, clearance **unresolved**. Before public
activation the implementation/lead lane must resolve Twitch sharing/storage
permission and incomplete/mixed-data presentation, YouTube derived-max and
cross-owner aggregation constraints, source branding, refresh/delete/backup
retention and client/project allocation. Removing payment does not remove
these constraints. No vendor outreach, legal commitment, policy exception,
project creation or quota purchase is authorized by this report.

## Evidence and reproducibility

Sanitized machine evidence root:
`C:/Users/bkam/AppData/Local/hermes/evidence/vr-live-stage1-gates-20261017/`.

- `o6-observed.json`: actual offline probe, no handle/chat/room payload stored.
- `o6-library-source.json`: inspected library versions and retrieval source.
- `f1-safety.json`: read-only scheduler metadata, historical coverage counters,
  documented-cost calculation, null project metric and explicit zero-call ledger.
- `twitch-excerpts.json`: canonical DOM excerpts with section anchors/date.
- `youtube-terms-excerpts.md`, `youtube-policies-excerpts.md`: retrieved public
  source snapshots; `youtube-branding.txt`, `youtube-quota-source.md`,
  `youtube-search-source.md`: supporting official sources.

The committed sample, numeric findings, matrix and source quotations above are
sufficient to review dispositions without access to that machine. Local evidence
is supporting provenance, not a substitute for a committed result. No personal
chat, webhook, API credential, room dump or registration database was exported.

[TW4]: https://legal.twitch.com/en/legal/developer-agreement/#iv-developer-accounts-and-rate-limits
[TW6]: https://legal.twitch.com/en/legal/developer-agreement/#vi-data-policy
[TW7]: https://legal.twitch.com/en/legal/developer-agreement/#vii-maintaining-the-integrity-of-program-materials
[TW11]: https://legal.twitch.com/en/legal/developer-agreement/#xi-developer-representation-and-warranties
[TW13]: https://legal.twitch.com/en/legal/developer-agreement/#xiii-additional-terms
[TWS]: https://legal.twitch.com/en/legal/developer-agreement/#schedule-1
[YT10]: https://developers.google.com/youtube/terms/api-services-terms-of-service#brand-features-and-attribution
[YT15]: https://developers.google.com/youtube/terms/api-services-terms-of-service#usage-and-quotas
[YPA]: https://developers.google.com/youtube/terms/developer-policies#a.-api-client-terms-of-use-and-privacy-policies
[YPC]: https://developers.google.com/youtube/terms/developer-policies#c.-implementing-youtube-features
[YPD]: https://developers.google.com/youtube/terms/developer-policies#d.-accessing-youtube-api-services
[YPE]: https://developers.google.com/youtube/terms/developer-policies#e.-handling-youtube-data-and-content
[YPF]: https://developers.google.com/youtube/terms/developer-policies#f.-user-experience
[YPL]: https://developers.google.com/youtube/terms/developer-policies#l.-additional-policies-on-derived-metrics-and-data-storage
[YDER]: https://developers.google.com/youtube/terms/derived-metrics-policy#acceptable-metrics
[YBI]: https://developers.google.com/youtube/terms/branding-guidelines#youtube_icon
[YBS]: https://developers.google.com/youtube/terms/branding-guidelines#logo_sizing_and_placement
[YQ]: https://developers.google.com/youtube/data/determine_quota_cost
[YS]: https://developers.google.com/youtube/data/reference/rest/v3/search/list

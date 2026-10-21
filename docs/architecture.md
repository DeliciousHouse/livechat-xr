# Why LiveChat XR has two display paths

A PCVR game can accept an OpenXR layer. A standalone Quest game cannot load this Windows DLL. LiveChat XR shares chat reading and batching, then sends text to the display path available to the player.

## The flow

```text
TikTok LIVE (TikTokLive + signer) / Twitch (anonymous TLS IRC)
                              |
                      app/chat.py relay
                              |
              +---------------+----------------+
              |                                |
       Windows tray app                 hosted relay server
       app/livechat_xr.py                 server/server.py
              |                                |
    atomic banner.txt + config            per-registration runner
              |                                |
    implicit OpenXR DLL                   Discord webhook POST
    layer/livechat_xr_layer.cpp                 |
              |                         Discord app on Quest
    chosen PC game, D3D11                notification display
              |
    head-locked quad over frame
```

The PC app can also post to Discord when its optional webhook is set. Leave that empty if the hosted relay already feeds the same channel, or messages can arrive twice. The PC path does not use the hosted relay or its billing.

## PC boundary

The tray owns networking, batching and settings. The layer owns drawing inside the game. An atomic UTF-8 text file is a small interface between Python and C++: no shared networking implementation and no IPC daemon. The cost is polling every 200 ms and retaining only the newest banner, not chat history.

The layer selects executable basenames at its first handshake. Only matching processes enable hooks/drawing. It creates a VIEW-space quad, so the banner moves with your head, not the world. A D3D11 texture is uploaded on the render thread and appended to the game's submitted frame layers. Unsupported graphics bindings, setup/upload errors or a rejected layer disable drawing and fall back to the game's original frame. This limits overlay disruption; it is not an anti-cheat exemption or universal game compatibility guarantee.

## Relay boundary

The relay uses Python's threaded HTTP server and one asyncio loop for stream connections. Registration requests persist JSON under a lock and schedule per-registration chat tasks. Each registration has an ordered Discord sender; HTTP posts run off the event loop rather than blocking chat collection. Startup migrates legacy plans, restores every registration with its channel limit and staggers connection attempts to reduce signing-service bursts.

A small JSON store avoids a database service. Atomic file replacement avoids partial files, but this is a single-process design: do not run multiple replicas against one volume. Discord chat posts preserve arrival order in chunks of up to five lines and 2000 UTF-16 units. Failed posts retry (429 honors Retry-After); attempts are capped at 25 per rolling minute. Only unsent backlog older than 60 seconds is summarized as `+N earlier messages`. The PC banner retains its independent three-line limit and `+N more` summary. Queues are in memory, not a durable chat archive.

## Private discovery: D1, one process and one volume

`server/network.py` is one `Network` on the existing relay asyncio loop, not a
second bot container or a census cron. Threaded HTTP verifies Discord signatures,
ACKs immediately and schedules async follow-up; collector/Discord HTTP and SQLite
work run off-loop with bounded network timeouts. Hosted chat keeps its existing
per-registration queues. `server/deploy.py` is unchanged.

```text
Discord interactions -> HTTP thread -> Ed25519 / private scope -> relay asyncio loop
Twitch / YouTube APIs -> to_thread -> complete poll -> /data/listings.db
                                                     | durable: listings + supplied_channels
                                                     | ephemeral: channels + live_streams
                                                     | process memory: per-channel peaks
relay loop -> source incident state -> /admin panel + one founder DM per transition
listings.db -> SQLite backup temporary copy -> sanitize / VACUUM / integrity + FK
            -> atomic listings.snapshot.db -> nightly archive (JSON/logs preserved)
stopped restore -> expiry -> unbound listings -> fresh ownership proof before rebind
```

Schema version 1 migrates local foundation `presence` transactionally, preserving
consent/review and marking old collector identities unverified. It never invents
supplied handles. Only an authenticated ownership-verification flow may record a
streamer-supplied handle or rebind its platform identity. Restored consent/review
alone cannot make a public entry. Removal deletes platform rows/bindings and
memory peaks immediately; removed listings expire at seven days. Collector data
and peaks expire within 24 hours; a completed empty poll removes current live
rows immediately. Counts remain separate, and unknown current primary counts
cannot use secondary counts or peak fallback.

This is repository implementation, not public activation, quota/terms clearance
or installed backup proof. Full list/ownership/manage UX belongs to the surfaces
lane; TikTok presence and T15 payments remain excluded. See [the core contract and
test ledger](network-operations.md) and [operational limits](operations.md#discovery-backup-and-restore).

## Identity and payment

A private manage token is the access credential, not an account password. The webhook identifies an existing registration when updating it.

New sign-up is **Log in or sign up with Discord** (owner decision 2026-10-21 02:26 PDT: "discord sign in works as they captures their info. so the sign up is login or signup with discord"). One Discord consent with scopes `identify email webhook.incoming` both identifies the streamer and creates the channel webhook. The registration stores the Discord user ID, username, display name and email when Discord shares a verified one. Pressing the button with no channels, or **Already set up? Log in** (`/recover`), asks only for `identify`, creates no webhook and opens the manage page of the relay linked to that Discord account. One Discord account can own up to 3 relays (`REGS_PER_DISCORD_USER`, one per stream); signing up the same stream again moves that relay to the newly picked channel instead of duplicating it. `MAX_REGS` stays the global ceiling, and the per-client rate limit on `/register` and `/recover` stays. With Discord OAuth configured, a pasted webhook can no longer create a registration; it can still replace the webhook of an existing one under **Edit settings**. Without Discord OAuth (self-hosted, local development) the page keeps the typed name/email and pasted webhook form; typed email is not verified. Google sign-in was removed: nothing else used it.

The OAuth `state` is single-use, expires after 900 s and is bound to the browser that started it by a `lcx_oauth` cookie (`SameSite=Lax; HttpOnly; Secure`, scoped to the callback path). A callback whose cookie doesn't match is refused with a "start again in this browser" page; that happens when Discord on a Quest or phone finishes the consent in another browser. The private listing flow's identify callback uses the same binding.

Relays created before Discord sign-in keep working with their manage link. Their owner links Discord by signing up again with Discord for the same stream in the same Discord server: the existing relay (plan, founder slot, creation date) moves to the new webhook and gains the Discord user ID. A relay already linked to a different Discord account is never handed over.

The manage link is posted into the connected Discord channel on every connect, and anyone who can read that channel can use it (security review M5). This is an accepted owner decision, 2026-10-21 02:26 PDT: "keep posting in discord". Use a private channel you control.

Founders run all saved channels free forever. Later free registrations run their first channel immediately; a signed Stripe paid checkout starts all saved channels. Subscription deletion returns paid users to one free channel without losing extra saved channels. Deletion and subscription cancellation are separate actions because the relay is not a Stripe billing client. Keep tokens and webhook URLs private. See [reference](reference.md) for the exact HTTP/config interface and [user guide](user-guide.md) for cancellation.

## What this does not prove

Discord receiving a message proves relay transport, not a notification over Population: ONE. Official Discord installation is documented, but actual standalone in-game pop-ups and a timed five-minute setup remain unverified in the retained newcomer QA. Native handshake/raster tests likewise do not prove every PC runtime/game combination. See the [verification notes](development.md#verification-notes) before publishing compatibility claims.

No rejected architectural alternatives are recorded in the checked-in comments; the trade-offs above describe the implementation, not an invented decision history.

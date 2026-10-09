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

The relay uses Python's threaded HTTP server and one asyncio loop for stream connections. Registration requests persist JSON under a lock and schedule per-registration chat tasks. Discord posting runs on short-lived threads rather than blocking the chat coroutine. Startup restores non-pending entries and staggers connection attempts to reduce signing-service bursts.

A small JSON store avoids a database service. Atomic file replacement avoids partial files, but this is a single-process design: do not run multiple replicas against one volume. Failed Discord posts are dropped instead of building an unbounded queue. A busy chat is summarized into a batch rather than delivering each message separately.

## Identity and payment

A private manage token is the access credential, not an account password. The webhook identifies an existing registration when updating it. Google optionally verifies name/email; manually entered email is not verified. Discord authorization can create the channel webhook instead of requiring manual copying.

Free registrations run immediately. Later registrations wait for a signed Stripe paid-checkout event. Deletion and subscription cancellation are separate actions because the relay is not a Stripe billing client. Keep tokens and webhook URLs private. See [reference](reference.md) for the exact HTTP/config interface and [user guide](user-guide.md) for cancellation.

## What this does not prove

Discord receiving a message proves relay transport, not a notification over Population: ONE. Official Discord installation is documented, but actual standalone in-game pop-ups and a timed five-minute setup remain unverified in the retained newcomer QA. Native handshake/raster tests likewise do not prove every PC runtime/game combination. See the [verification notes](development.md#verification-notes) before publishing compatibility claims.

No rejected architectural alternatives are recorded in the checked-in comments; the trade-offs above describe the implementation, not an invented decision history.

# LiveChat XR Roadmap (2026-10-10)

Goal: turn LiveChat XR from a free PCVR overlay with 3 relay sign-ups into a paid product for VR streamers by 2027-01-15, without Brendan becoming its full-time maintainer. Open source stays open; the hosted relay and TTS are what people pay for.

## Where it stands
- Shipped: PC app (OpenXR head-locked banner, D3D11), TikTok/Twitch/YouTube chat, gifts in gold, TikTok follow-ask (a banner line shown to the streamer, who says it on mic), Discord webhook output; hosted relay at livechat.deliciouswines.org with sign-in, Discord OAuth, admin page, nightly backups, Stripe billing (5 free slots then $3/month). MIT on GitHub (DeliciousHouse/livechat-xr).
- Relay sign-ups 2026-10-10: 3 (all TikTok, all free, all in the last 48 h, no marketing). Two free slots left.
- Finding (livechat-xr-mobile, 2026-10-06): Horizon OS never lets one app draw over another app's immersive game. A Horizon Store app cannot be an in-game overlay. Quest shows notifications over games (Discord today) and, to verify, background audio. So the standalone product is audio, not pixels.

## Decisions
1. Everything on a user's device stays open source in one repo (PC app, OpenXR layer, any Quest/phone client). No codebase split.
2. The hosted relay stays private and is the product: accounts, billing, uptime, follow-ask logic, TTS, sign service. Self-hosting from the Dockerfile stays possible.
3. The paid feature is chat read aloud (TTS), not multi-channel. Follow-ask customization, gift/follow alerts and co-host merge ride the same tier.
4. The Horizon Store app is a companion (relay sign-in, channels, TTS as background audio), not an overlay. Ships only after the background-audio probe passes.
5. The free tier carries the growth loop: one channel, chat to Discord/banner, default follow-ask with a visible "LiveChat XR" tag on the stream-visible banner (the stream IS the headset view) and on free-plan relay Discord posts.
6. Agents do the upkeep (relay-ops Hermes profile once paying users exist). Brendan's hours stay under ten a month.

## Phases and gates (a failed gate cancels the phase, it does not delay it)
- Phase 0 Validate (Oct 10 - Oct 24): hourly VR LIVE census (Hermes cron vr-live-census, weekly rollup), BeamXR/Meta threat analysis (kanban t_7450fd71), ship the tag + per-stream stats, fill the 5 free slots.
  GATE 1: 150+ census regulars (handles seen 3+ days) AND threat verdict is not "partner or get out". Fail -> free-and-open with a donation link; stop here.
- Phase 1 Monetize (Oct 24 - Nov 14): TTS on PCVR + relay, price to $4.99, relay-ops profile, first paying users.
  GATE 2: Quest background-audio probe passes (does a 2D app's audio keep playing inside an immersive session?).
- Phase 2 Quest (Nov 14 - Dec 19): companion app (relay sign-in, channels, TTS), Horizon Store submission (VRC subset, 2+ week review).
  GATE 3: 10 paying users.
- Phase 3 Grow (Dec 19 - Jan 15): gift/follow alerts, custom follow-ask, co-host merge, BeamXR outreach, go/no-go on scale.

## Backlog (status as of 2026-10-10)
| Feature | Platform | Tier | Status | Gate |
|---|---|---|---|---|
| Banner, gifts, 3 chat sources, follow-ask | PCVR + relay | Free | Shipped | - |
| Relay: Discord, sign-in, Stripe, admin, backups | Relay | Internal | Shipped | - |
| VR LIVE market census | Hermes cron | Internal | Running | - |
| BeamXR / Meta threat analysis | Hermes kanban | Internal | Running | - |
| "LiveChat XR" tag on free banner + free-plan relay posts | PCVR + relay | Free | Next | none |
| Per-stream stats sent to the streamer at stream end | PCVR + relay | Free | Next | none |
| Quest background-audio probe | Quest | Internal | Next | none (needs the Quest 3 in hand) |
| Chat read aloud (TTS) | PCVR + relay | Paid | Gated | Gate 1 |
| relay-ops Hermes profile | Hermes | Internal | Gated | first paying user |
| Quest companion app + Horizon Store listing | Quest | Paid | Gated | Gate 2 |
| Phone companion (mirrored notifications) | iOS/Android | Free | Later | only if the audio probe fails |
| Follow-ask customization, gift/follow alerts | Relay | Paid | Gated | TTS shipped |
| Co-host merge | Relay | Paid | Later | Gate 3 |
| BeamXR partnership outreach | - | Internal | Gated | threat verdict |

## Pricing
Free: 1 channel, chat to Discord/banner, default follow-ask with tag, summary stats. Paid ($3 now, $4.99 once TTS ships): 3 channels, custom follow-ask without tag, TTS, alerts, full stats, co-host merge later. Quest app bills through the Horizon Store. Self-hosting stays free.

## Risks
BeamXR/Meta ship chat-in-headset (measured by the threat card); market too small (measured by the census; <150 regulars = free-and-open); TikTok reader breakage via Euler Stream (relay-ops, status line, paid Euler key before the 20th payer); Quest background audio unverified; Horizon Store review; anti-cheat exposure of the OpenXR layer (TTS has none).

# LiveChat XR

See your **Twitch** or **TikTok LIVE** chat while you play PC VR. New comments appear as a short banner near the top of
your view, pinned to your head so it stays visible however you turn, then fade after a few seconds.

**Gifts and support show too, in gold:** TikTok gifts (a gift combo is shown once, with its final count), Twitch Bits
cheers, subs, resubs and gifted subs (a mass gift is shown once, not once per sub). Gifts are always listed first in a
banner, so they never get folded into "+N more".

Comments that arrive close together share one banner ("+N more" when there are lots), so a busy chat can't bury your view.
Your own messages are skipped.

## Standalone Quest? Use Discord instead (no PC)

Go to **https://livechat.deliciouswines.org**, enter your TikTok or Twitch channel and a Discord webhook URL, and your
chat gets posted to that Discord channel whenever you're live. With the Discord app on your Quest, comments pop up as
notifications in-game. The page walks you through making the webhook. You get a private link to send a test or stop it,
and that link is also posted in your Discord channel.

Usernames can include `@` or a full TikTok/Twitch profile URL. Sign-up checks TikTok profiles, including offline accounts;
if TikTok is unavailable, sign-up still works with a note on the manage page. Twitch names are checked for valid syntax
only (its account lookup needs API credentials). Repeated TikTok account-not-found errors retry every 30 minutes after
five consecutive failures; the manage page and admin dashboard show the problem. Correct a channel by signing up with
the same Discord webhook to update the existing registration without losing its plan or free spot.

Self-hosting the relay: `docker build -f server/Dockerfile -t livechat-xr-relay .` then
`docker run -d -p 13300:13300 -v livechat-xr-data:/data -e PUBLIC_URL=https://your.host livechat-xr-relay`.

## How it works

LiveChat XR has two parts:

1. **A tray app** connects to your chat and writes each batch of comments to `%LOCALAPPDATA%\LiveChatXR\banner.txt`.
2. **An OpenXR API layer** loads into the VR games you choose and draws that text as a head-locked panel on top of the game.
   It never touches games you haven't listed.

## Requirements

- Windows 10/11, x64
- A PC VR game that uses **OpenXR with Direct3D 11**. Direct3D 12 and Vulkan games are not supported yet; the layer
  stays out of their way.
- Any OpenXR runtime: Meta Quest Link / Air Link, Virtual Desktop (VDXR), SteamVR, and so on.

| Game | Runtime | Status |
|---|---|---|
| Population: ONE (Meta PC version) | Quest 3 via Virtual Desktop | ✅ Tested |
| Anything else | | Untested: please [report results](https://github.com/DeliciousHouse/livechat-xr/issues) |

## Install

1. Download `LiveChatXR-Setup-x.y.z.exe` from [Releases](https://github.com/DeliciousHouse/livechat-xr/releases) and run it.
   The installer needs admin rights because it registers the OpenXR layer machine-wide (some game loaders ignore
   per-user registration).
   - The installer isn't code-signed yet, so Windows SmartScreen may warn you. Click **More info → Run anyway**.
2. On first launch, the settings window opens:
   - **Platform**: Twitch or TikTok LIVE
   - **Channel / username**: your Twitch channel or TikTok @handle
   - **Games**: the game's exe name, e.g. `PopulationONE.exe`. To find it, open Task Manager while the game is running,
     go to the Details tab, and read the exe name.
3. Start the game, then use **Send test banner** in the tray menu (or the settings window). You should see it in your headset.

Placement (height, distance, width) and the game list are read when the game starts, so restart the game after changing them.

### Optional: Discord (chat on your Quest without the PC overlay)

LiveChat XR can also post each chat batch to a Discord channel. The Discord app on your Quest then pops it up as a
notification, even mid-game, so it works for standalone Quest games too.

1. In Discord, use a server you own (a private one is fine) and make a channel just for this, e.g. `#stream-chat`.
2. Channel settings (gear icon) → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
3. Paste it into **Discord webhook URL** in LiveChat XR settings and save.
4. Install Discord on the Quest, sign in, and set that channel's notifications to **All Messages**. Mute your other
   servers while streaming if you only want chat pop-ups.
5. **Send test banner**: the same text should land in the channel and pop up on the Quest.

Treat the webhook URL like a password: anyone who has it can post to that channel. Mentions are always disabled, so chat
text can never ping @everyone or roles. LiveChat XR posts at most once per banner window (7 s by default), well under
Discord's limit of about 30 posts a minute per channel.

## ⚠️ Anti-cheat

The layer runs inside the game process, as every OpenXR overlay does (OpenKneeboard, OpenXR Toolkit, and others). Some
anti-cheat systems may object to that. Only add games where third-party OpenXR overlays are allowed. You use it at your
own risk.

## Troubleshooting

All logs are in `%LOCALAPPDATA%\LiveChatXR\` (tray menu → **Open data folder**).

| Symptom | Check |
|---|---|
| No banner, `overlay.log` missing or no new line | The game isn't in the Games list (exact exe name), or it isn't an OpenXR game |
| `overlay.log` says `DISABLED: not a D3D11 session` | The game uses D3D12/Vulkan, which isn't supported yet |
| `overlay.log` has `session ready` but no banner | Check that the tray status says "connected" and that comments are arriving (`app.log`) |
| TikTok says retrying / rate limited | TikTok is read through the unofficial [TikTokLive](https://github.com/isaackogan/TikTokLive) library, which uses Euler Stream's sign service. Heavy users can get a free key from [eulerstream.com](https://www.eulerstream.com) and paste it into settings |

**Kill switch:** set the environment variable `LIVECHATXR_DISABLE=1`, or uninstall. Uninstalling removes the layer registration.

## Build from source

Needs Visual Studio 2022 Build Tools (C++), Python 3.12, and Inno Setup 6.

```bat
layer\build.cmd
python -m venv .venv && .venv\Scripts\pip install -r app\requirements.txt pyinstaller
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\pyinstaller --noconfirm --windowed --name LiveChatXR --collect-all TikTokLive --collect-all TikTokLiveProto --collect-submodules EulerApiSdk --paths app app\livechat_xr.py
"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" /DAppVersion=0.1.0 installer\livechat-xr.iss
```

CI builds the installer on every push, and pushing a `v*` tag publishes a GitHub release.

## License

MIT for LiveChat XR's code. Bundled components keep their own licenses; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
Not affiliated with TikTok, Twitch, Meta, or the Khronos Group.

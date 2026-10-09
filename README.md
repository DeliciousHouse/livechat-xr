# LiveChat XR

Read TikTok LIVE or Twitch chat while playing VR. Choose the path that matches where your game runs.

| Path | How it works | Start here |
|---|---|---|
| **Windows PCVR overlay** | A tray app reads chat; an OpenXR layer draws short, head-locked banners in selected D3D11 games | [Install and first run](docs/user-guide.md#pc-first-run) · [Download installer](https://github.com/DeliciousHouse/livechat-xr/releases) |
| **Quest standalone via Discord** | The hosted relay posts chat to your Discord channel, with display handled by Discord on Quest; no LiveChat XR PC app | [Quest setup and notification test](docs/user-guide.md#quest-first-run) · [Connect relay](https://livechat.deliciouswines.org/) |

**Check compatibility first:** PC requires Windows 10/11 x64, an OpenXR/Direct3D 11 game and administrator rights to install. D3D12 and Vulkan are unsupported. The repo previously recorded the Meta PC version of Population: ONE on Quest 3 via Virtual Desktop as tested; other combinations need testing. Standalone in-game Discord pop-ups and timed five-minute setup remain unverified in newcomer QA. Follow the guide's test checkpoint before relying on chat during a stream.

The PC banner batches comments (seven seconds and three selected lines by default), prioritizes gifts/support in gold, summarizes overflow and skips your own normal comments. It is not a full chat-history viewer. The PC app can optionally also post to Discord; leave that field empty if your hosted relay already feeds the same channel.

## First steps

- **PC:** download `LiveChatXR-Setup-<version>.exe` from release **Assets**, run it, select platform/handle in Settings, save, start the chosen game and **Send test banner**. The installer is unsigned; verify its source before accepting a Windows warning. Restart the game after placement/game-list changes.
- **Standalone:** install Discord from Meta Horizon Store, sign in, connect your channel at the hosted relay, then **Send test message** on your private manage page. Verify the actual Discord channel first, then notifications outside and inside the game. No unofficial APK is required.
- **Hosted pricing:** first five available free registrations, then **$3/month** or **$25/year**. Your manage page confirms whether payment is needed. The PC app has no subscription gate. [Billing and cancellation](docs/user-guide.md#pricing-and-billing) · [Privacy](https://livechat.deliciouswines.org/privacy).

## Documentation

| Document | Use it for |
|---|---|
| [User guide and FAQ](docs/user-guide.md) | Illustrated first runs, webhook setup, name correction, billing, cancellation and troubleshooting |
| [Reference](docs/reference.md) | Settings/defaults, files, commands, environment variables and HTTP interface |
| [Architecture](docs/architecture.md) | How the PC layer/app and hosted relay share chat, plus limits and trade-offs |
| [Development](docs/development.md) | Local environment, tests, empty local relay and verification evidence |
| [Operations](docs/operations.md) | Dora service map, admin/backup diagnosis and canonical release/deploy runbook ownership |
| [Contributing](CONTRIBUTING.md) | Scoped changes, draft PRs, tests and safe reports |

## Safety and support

The PC layer runs inside the game process. Only enable it where third-party OpenXR overlays are allowed; anti-cheat compatibility is not guaranteed. Uninstall removes its machine-wide registration. `LIVECHATXR_DISABLE=1` in the game's environment is the layer kill switch.

Webhook URLs, private manage links, keys, config files and backups can grant access or contain private data. Do not post them in support issues. **Stop and delete does not cancel paid billing**; use the Stripe billing portal separately. See [troubleshooting](docs/user-guide.md#troubleshooting) or [report a problem](https://github.com/DeliciousHouse/livechat-xr/issues) with sanitized steps and your platform/version.

For isolated relay simulation and visible Windows Settings without touching the installed profile,
see [QA fixtures](docs/qa-fixtures.md). CI also uploads `LiveChatXR-Portable` for alternate-data testing
without running an installer.

## License

MIT for LiveChat XR's code. Bundled components keep their own licenses; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Not affiliated with TikTok, Twitch, Meta or the Khronos Group.
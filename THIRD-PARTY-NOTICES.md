# Third-party notices

LiveChat XR's own code is MIT-licensed (see `LICENSE`). The installer also ships the components below.

| Component | License | Notes |
|---|---|---|
| [OpenXR SDK headers](https://github.com/KhronosGroup/OpenXR-SDK) | Apache-2.0 | Vendored in `layer/include/openxr` (compile-time only) |
| [TikTokLive](https://github.com/isaackogan/TikTokLive), TikTokLiveProto | AGPL-3.0 with additional permissions (Section 18) | The authors' Section 18 exception allows integration into TikTok LIVE overlay tools without adopting AGPL-3.0, as long as the tool is not a closed-source/hosted service (Section 19). LiveChat XR is an open-source desktop app. |
| EulerApiSdk | not stated | Dependency of TikTokLive (Euler Stream sign API client) |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | Unmodified; source at the link |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | |
| httpx, httpcore, websockets, protobuf, idna, python-dateutil | BSD-3-Clause | |
| pydantic, attrs, anyio, pyee, betterproto2, six, ffmpy, websockets_proxy, h11 | MIT | |
| mashumaro, python-socks, async-timeout | Apache-2.0 | |
| certifi | MPL-2.0 | |
| CPython runtime (bundled by PyInstaller) | PSF-2.0 | |
| [Pretext](https://github.com/chenglou/pretext) | MIT | Relay server only: vendored in `server/pretext.js`, served to the sign-up page |

TikTok and Twitch are trademarks of their owners. LiveChat XR is not affiliated with either. TikTok chat is read
through an unofficial library and can break when TikTok changes its site.

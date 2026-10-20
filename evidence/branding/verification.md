# Approved brand integration — source evidence

Card: t_1e8844d5. Base: 7b751cc (fresh origin/main).

## Acceptance ledger

- Imported the approved svg/png/windows/web/social/font/source directories and raster provenance. All 52 source export manifest hashes verified before copying. The repository manifest contains the 40 imported asset/provenance files and original pack SHA256; Git attributes preserve their exact bytes across OS checkouts. Marketing QA/mockups are not imported as installed proof.
- LiveChatXR.spec resolves paths from SPECPATH, retains TikTokLive/TikTokLiveProto/EulerApiSdk collection, embeds the ICO and bundles runtime PNG/ICO data. CI packages this checked-in spec and runs the frozen --check resource smoke test before compiling the installer.
- Installer SetupIconFile uses the approved ICO; existing AppId, privileges, registry, uninstall EXE display icon, shortcuts and application-closing semantics are unchanged.
- Tray uses dedicated 32 px PNG; Tk root/default and Settings use ICO. Source/frozen discovery, missing PNG and mocked native icon calls covered without starting the tray or touching owner settings/registry. --check source execution passed.
- Relay serves five exact allowlisted file routes with explicit image MIME and nosniff. Actual loopback GET bytes/hashes, traversal/unknown paths and missing-file failure covered. Docker copies web exports under /srv/assets/brand/web; source layout reads repo assets. No arbitrary request-derived filesystem path.
- All shared HTML pages reference favicon ICO/SVG, apple touch icon and accessible outlined header. New header wraps navigation rather than shrinking the wordmark below its 195 px minimum. Existing functional DESIGN.md palette/type and flows remain unchanged.
- Discord username/disabled-mentions behavior is verified with mocked transport. Automatic avatar replacement is intentionally unsupported: current webhook sender lacks an identity opt-in/custom-avatar check. An existing user's avatar is not overridden; the public avatar resource is prepared, not claimed live. No discovery bot identity changes.
- README uses final art on a fixed dark background for readability in light/dark document viewers; docs/brand.md carries provenance, OFL, runtime layout, original integration contract and downstream limitations. Third-party notice includes Outfit.

## Executed checks

- New test_brand suite: 8 tests pass. Original RED run failed missing resources/spec/helper/static GETs, then PASS after implementation.
- layer/build.cmd: native DLL and runtime test compile successfully; no installation/registration.
- Full current Python 3.12 suite: 254 tests, OK (5 platform skips). Existing resource/event-loop warnings remain. One earlier run failed the pre-existing response/event ordering race in test_qa_fixtures.py:125; isolated rerun and final full run passed without changing that test or the stop ordering. The handler's MIME-forwarding adaptation is independently covered by binary/SVG fixture GETs.
- npm run verify: unavailable (Missing script: verify); repository is Python and documents unittest/native/packaging gates instead.
- git diff --check: source changes pass; the imported upstream OFL has one original trailing space, intentionally preserved to retain its approved hash.
- Local simulated relay rendered at 1280x900 and 390x844: header image loaded, no horizontal overflow. Vision inspected both captures and found no concrete changed-header defect. Fixture banner/CSP are intentional. These are rendered source UI checks, not production/deployment/native installation acceptance.
- Ship checklist Pass 1 (trust boundaries, paths, gates, concurrency) and Pass 2 (completeness/distribution/docs): no unresolved code findings. CI gate changes only switch packaging to the spec; no test assertion or required gate relaxed. Similarity flag below remains a canonical-review obligation, not a code pass.

## Remaining gates

Exact-head Windows packaging/installer and Linux aggregate CI must be green; run/artifact IDs belong in the PR/card handoff, not inferred from local checks. Canonical reviewer must inspect assets/brand/svg/livechat-xr.svg plus native 16/32 art: violet + white two-eyed silhouette can evoke Discord. This is not trademark/legal clearance, and the approved concept is not silently changed.

Source integration only. Canonical reviewer owns merge; Dora t_5041b970 must verify reviewed deployed SHA/bytes/MIME through the existing controller. Native QA t_11cdf1d6 must inspect actual installer/EXE/shortcut/tray/Settings/uninstall UI in an isolated Windows environment. Both remain promo prerequisites. No actual webhook, owner install, production mutation, social upload, promo, headset or five-minute go-live acceptance occurred.

Rollback: revert the reviewed branding commit and rebuild/redeploy via existing lanes; retain user data and settings.

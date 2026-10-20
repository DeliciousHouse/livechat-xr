# LiveChat XR brand integration contract

Asset phase only. Marketing must not claim this task's installer/app/live acceptance before the downstream lanes supply it. Use approved Visor App Tile, #6D28D9; do not ask for the direction again. Carry BRAND.md similarity flag to sole canonical reviewer.

## Source layout decision

Import this package under repo `assets/brand/`, preserving svg/, png/, windows/, web/, social/, font/ and source/. Brand guide may live in `docs/brand.md` with checked links. Keep generated QA/contact sheets and review evidence in the evidence tree unless a review artifact is useful to the repo. Verify hashes from manifest.json. Preserve approved raster provenance in documentation, not an undocumented rename of approved_variant C / variant-A3.png.

## dev-eng owns implementation and PR

Work from fresh remote main in an isolated registered project worktree; the primary local checkout was stale at 7ccd84be503282c92336f8d2291ae5d83b5544ed during this export task. Do not modify that checkout in place. No force-push or author merge. Use DeliciousHouse/livechat-xr and existing canonical dev-reviewer intake; don't spawn competing PR reviewers.

1. LiveChatXR.spec: add the ICO to EXE icon= and bundle required PNG/ICO runtime data. Resolve paths from the spec/repo, not cwd.
2. installer/livechat-xr.iss: SetupIconFile points to the asset ICO. Keep UninstallDisplayIcon={app}\\LiveChatXR.exe so the uninstall display uses the branded EXE; preserve AppId and current registry/install semantics. No installer privilege changes.
3. app/livechat_xr.py: replace generic ImageDraw icon_image with bundled approved PNG loading. Handle both source and PyInstaller frozen resource paths. Apply icon to Tk root and Settings windows as well as pystray. Keep portable mode isolated; no user's existing settings/registry changes in tests.
4. server/server.py: serve favicon.ico and favicon.svg and header SVG from packaged files with explicit correct MIME types; set HTML icon links and accessible header markup. Include assets in container build context/image. Do not embed uncontrolled filesystem paths or permit arbitrary file traversal. Test actual GET responses, not just string presence. Maintain signup/manage/live/admin behaviour and CSP.
5. Discord webhook: app/chat.py post_discord already sets username='LiveChat XR' but no avatar_url in observed source. Add public stable HTTPS avatar URL only if supported by current webhook flow and approved product privacy constraints; serve web/discord-avatar.png at that URL. Do not leak a webhook, override a user's custom avatar unexpectedly, change Discord bot identity, or send production test messages. Unit-test payload with mocked transport. Document any intentional unsupported path; avoid claiming actual avatar readback without a safe fixture.
6. Docs: README/user guide use final logo, not concept artwork. Coordinate previously completed docs t_f2fbf0e7 and CI/CD t_7fe554c8 by updating their resulting source paths, not re-opening completed phases casually.
7. GitHub/TikTok/Reddit exports are prepared files. Do not upload to public social profiles or publish promo. GitHub social preview settings update is not assumed from including its file in the repo.

Add regressions covering bundled resource discovery, missing-resource error behaviour, ICO sizes/native frames, site static routes/content types and webhook payload without real network. Run current full suite, packaging and installer gates at the exact PR head. Include source audit, test results and CI handles in PR handoff. CI success is not native visual acceptance.

## Dora owns reviewed deployed-site verification

Wait for sole-reviewer merge and exact-main green CI. Use existing pull-deploy controller, not a competing manual deployment. Verify deployed SHA, home/header and favicon/avatar GET bytes and MIME types against committed assets; keep previous image and backup. Report exact URLs, SHA, status and hashes. Do not modify registrations, payment state or production webhooks. This is branding acceptance only, not new chat/headset or launch proof.

## dev-qa owns actual Windows visual acceptance

Use the actual reviewed installer artifact, record SHA and version. Inspect installer icon; installed EXE; Start menu shortcut; tray; Settings title/taskbar; Programs & Features/uninstall icon. Screenshots must show real native UI, not review.html or Windows Explorer thumbnail alone. Use an authorized disposable Windows environment/isolated account; never install over Brendan's running instance, stop his app, change HKLM/OpenXR registration on his live PC or bypass UAC boundaries to get a screenshot. If a required isolated elevated test environment is absent, report that exact capability gap to controller. Retain unverified surfaces explicitly rather than converting mocked UI/source tests to PASS. Check 16/32 px on actual light/dark taskbar when available.

## Closure and promo gate

Asset completion can release dev-eng but cannot release promo alone. Preserve t_817e6d0b -> t_635b32ed and add downstream source/release/visual-acceptance prerequisites to t_635b32ed. No posts, announcements, spend or headset/five-minute claims. Product readiness and owner go-live approval remain separate existing gates.

Rollback: revert reviewed branding commit and rebuild/redeploy using existing controller; do not delete user state, rotate secrets or reinstall over owner environment. Source paths and APIs must be re-traced on current main before editing because the observed local checkout is stale.

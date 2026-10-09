# CI, releases and relay deployment

## Pull requests and Windows releases

`.github/workflows/build.yml` runs on every PR (no path filters), main push and
`v*` tag. The required aggregate check is `full-suite`: Linux deployment guards
and loopback probe plus Windows Python 3.12 tests, the native x64 OpenXR DLL,
PyInstaller `--check`, and Inno Setup installer compilation. Pip is
cached; obsolete branch/PR builds cancel, tagged builds do not. PR builds have
read-only repository permissions. Only the separate tag release job can write.

The assigned reviewer merges after `full-suite` passes. Protect main with this
check, strict/up-to-date mode, enforced for admins, no force pushes or deletions.
Do not require formal self-approval: the team shares one GitHub identity.

Tag a reviewed main commit, for example `v0.2.1-ci.1` for a test pre-release.
The build uploads one installer and `SHA256SUMS`; the release job downloads and
verifies the checksum before publishing both assets with generated notes.
Suffixed tags (`-` or `+`) are pre-releases and never become latest. Re-running
the tag workflow updates the same release/assets. Never move an existing tag.
Download both assets and run `sha256sum --check SHA256SUMS` (PowerShell users:
`Get-FileHash LiveChatXR-Setup-*.exe -Algorithm SHA256`). SHA256 detects accidental
corruption; it is not code signing. Installer elevation and current icon/branding
acceptance remain separate PC release checks.

## Dora pull deploy

No GitHub-hosted runner can reach Dora, and no runner is installed on Dora.
The user systemd timer reads this public repo and public exact-head check status.
It needs no GitHub/tailnet key and cannot receive PR jobs. Only reviewed main with
a completed, successful latest `full-suite` is eligible. GitHub rate limits/errors
fail closed. The existing node Docker access is host-root-equivalent; never give
it to a runner. Stripe/Google/Discord secrets stay in `~/.livechat-xr.env` (0600).

Dora owns installation and the live operation card. From a reviewed source tree:

```sh
install -d -m 700 ~/.local/lib/livechat-xr-deploy ~/.local/share/livechat-xr-deploy ~/.config/systemd/user
install -m 700 server/deploy.py server/deploy_probe.py ~/.local/lib/livechat-xr-deploy/
install -m 600 server/livechat-xr-deploy.service server/livechat-xr-deploy.timer ~/.config/systemd/user/
# Populate ~/.local/share/livechat-xr-deploy/deployed with the independently verified
# 40-character revision of the CURRENT running relay, not the fetched candidate.
# If its source revision cannot be proven, stop and reconcile it first.
systemctl --user daemon-reload
systemctl --user enable --now livechat-xr-deploy.timer
```

Confirm Python >=3.12, user-manager persistence (linger), Docker access and no
competing deployment automation first. Existing daily backup cron stays; it is
not a deploy controller. The installer deliberately does not enable linger or
change host permissions. Install the controller from the reviewed commit; fetched
candidate source never replaces the controller automatically.

Every ten minutes it fetches main, checks exact-head CI, and compares the last
processed main revision. Changes under `server/` or to shared `app/chat.py` deploy;
PC/docs-only changes just advance the processed revision. Intermediate main
commits are coalesced. A file changed and then reverted before the next poll needs
no deploy. Deployment is serialized with flock; the timer never overlaps itself.

Sequence:

1. Verify the existing container's bridge `livechat-xr`, LAN-only
   `192.168.1.240:13300`, `livechat-xr-data:/data`, and `unless-stopped`. Drift stops
   deployment rather than dropping mounts/ports silently. Capture home 200,
   anonymous admin denied, all registrations and their current connections through
   loopback manage pages. Only hashed IDs leave the container; no manage URL,
   registration fields, secrets or raw errors are logged.
2. Build an immutable candidate image with `docker build --network host`. Never
   use host networking at runtime. Persist a recovery journal; briefly pause the
   old container and make a read-only, network-disabled volume backup. Read the
   archive's full contents/checksum and require `registrations.json`; unpause.
3. Stop/retain/rename the original container and start the candidate with the same
   network, port, volume and local env file. Wait up to four minutes for home/admin,
   all pre-existing registrations and every previously connected registration to
   reconnect. This is a brief restart, not a zero-downtime claim.
4. If start or health fails, remove only the new container and restart the retained
   original container. Verify the same continuity. Recover an interrupted backup
   or swap from the journal on the next invocation. Keep the journal if recovery
   fails. Do NOT restore the backup automatically: that would discard registrations
   or billing events written after swap. Schema migrations are not supported by
   this automatic path and must be separately scoped before merging.

Results: `~/.local/share/livechat-xr-deploy/result.json` contains the processed SHA
and continuity counts. Zero connections before deploy means no live connection
was proven; do not call it an existing-connected-user acceptance pass. Failure
marks the SHA in `failed` so the timer does not repeatedly restart live users.
Dora may remove that marker after investigating to retry the same immutable SHA.
Retained containers/images and 0700 backups are intentionally not automatically
pruned. Dora monitors free disk and retires them under its existing backup policy;
keep the last known-good image/container and at least one validated backup.

Status and emergency hold:

```sh
python3 ~/.local/lib/livechat-xr-deploy/deploy.py --status
systemctl --user status livechat-xr-deploy.timer livechat-xr-deploy.service
systemctl --user disable --now livechat-xr-deploy.timer
```

`--status` is non-deploying and fails until processed main matches remote main;
it also probes the live container. It does not assert a live streamer when none
is connected. Hold the timer before emergency manual rollback. Revert through a
reviewed PR for durable rollback; never force-push or restore live data casually.

## Required end-to-end evidence (not replaced by unit tests)

- A draft PR's exact head has green `full-suite`; main protection readback names it.
- After assigned review/merge, a new suffixed test tag publishes an installer,
  generated notes, pre-release=true/latest unchanged, and a matching SHA256 asset.
- Dora first rehearses backup/swap/forced-health-failure rollback with synthetic
  data in a cloned test volume, separate names, an explicit unused subnet and a
  loopback-only spare port. Disable outbound registration/callback effects; never
  mount production data read-write in a test container. The default controller is
  production-scoped: adapt its constants in the test copy only, never production.
- One reviewed relay deploy runs with preserved production configuration, home 200,
  admin denied and an actual existing connected registration reconnecting. Report
  counts only, not Pop's handle, tokens, webhook, email or admin page contents.
- Apply the protected deploy section below via the controller's interactive consent
  path, then record the actual deployment/tag results and vault record. Until then,
  source delivery alone is not end-to-end completion.

## Proposed AGENTS.md section (not applied by headless workers)

The existing Windows app and hosted API have different release triggers. The
custom hooks make relay verification wait for Dora's pull timer, not a runner.
The Windows tagged release is intentionally separate from merge-time deployment.

```markdown
## Deploy Configuration (configured by /setup-deploy)
- Platform: custom Docker on Dora + GitHub Actions Windows releases
- Production URL: https://livechat.deliciouswines.org
- Deploy workflow: .github/workflows/build.yml (Windows tag releases); Dora pull timer (relay)
- Deploy status command: ssh node@192.168.1.240 python3 .local/lib/livechat-xr-deploy/deploy.py --status
- Merge method: squash
- Project type: Windows app + API
- Post-deploy health check: https://livechat.deliciouswines.org/health

### Custom deploy hooks
- Pre-merge: gh pr checks --required
- Deploy trigger: automatic polling of reviewed main on Dora; Windows releases on v* tags
- Deploy status: ssh node@192.168.1.240 python3 .local/lib/livechat-xr-deploy/deploy.py --status
- Health check: https://livechat.deliciouswines.org/health
```

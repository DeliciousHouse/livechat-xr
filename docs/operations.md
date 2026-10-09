# How to operate the hosted relay

Use this index for diagnosis and the CI/CD-owned runbook for procedures that change the service. Never put secret values or customer records into a public runbook.

## Service map

The assigned deployment is Docker container `livechat-xr-relay` on Dora (`node@ubuntu-docker`), port 13300 behind Caddy for `https://livechat.deliciouswines.org`. Persistent volume is `livechat-xr-data`, mounted at `/data`. Runtime secrets are kept on Dora in `~/.livechat-xr.env`, not in the repo or GitHub Actions. This map comes from the deployment brief; it is not a claim that this docs run inspected the host.

The [Dockerfile](../server/Dockerfile) copies `app/chat.py` and `server/server.py`, installs TikTokLive, and runs as `nobody`. The data mount must be writable by that user. One process owns the JSON volume; do not scale replicas against it.

## Release and deploy owner

CI/CD card `t_7fe554c8` owns the canonical **docs/release-deploy.md** runbook. That file is being delivered separately; this docs branch does not invent or duplicate its commands. Follow it when available in the merged tree. Until that delivery is reviewed, deployment, backup/restore and rotation procedures are pending, not verified here.

The canonical runbook must cover:

- Windows `v*` tag releases, hyphen-suffixed pre-releases, installer assets, SHA256 and release notes.
- Dora deployment: backup volume, build/swap, health/registration continuity, automatic rollback and a rollback rehearsal. Dora requires host-network builds and an explicit network subnet because its default bridge/address pool is unavailable.
- Nightly server backups plus offsite pull to Windows `D:\Backups`, retention, freshness checks and restoring both data and a compatible image.
- Admin access, Stripe webhook setup/testing and secret rotation through approved storage without printing values.

Current tag behavior is visible in [build.yml](../.github/workflows/build.yml); deployment automation is not implied by the app build workflow. A tag or merge is a service/release action, not a harmless docs test.

## How to diagnose without modifying users

1. Open the public home and [privacy page](https://livechat.deliciouswines.org/privacy). Confirm they load. `/health` returning 200 only means the HTTP process responds, not that TikTok/Discord delivery works.
2. Open the private admin dashboard through approved secret access. `/admin` without the correct configured key returns 404; admin does not use a public account login. Do not paste its full URL into a command, screenshot, card or issue.
3. Compare live status, delivery/failure counts and last-post age for the affected connection. Distinguish **waiting for payment**, **waiting to go live**, account-not-found and Discord post failures. Red recent logs are clues, not proof of an outage for every user. The dashboard refreshes every 30 seconds.
4. Check **Backup on server** and **copy on PC** ages. Missing or more-than-36-hour-old timestamps warn. A fresh timestamp is not proof an archive can be restored. Use the canonical runbook to inspect retention and rehearse restore in an isolated target.
5. Ask the affected user to send a test from their own manage page and confirm receipt in the channel. Do not use another customer's manage token, change a live registration or send chat on their behalf as a routine health check. A Discord message still does not prove a headset notification.

## Data and recovery boundaries

`registrations.json` includes private manage tokens, webhooks, names/emails, stream channels, plans and subscription IDs. `stats.json` persists periodically; logs rotate. `last_backup` and `last_offsite` are epoch-time stamps written by external backup jobs, not backup jobs inside `server.py`. Keep backups protected as credentials and customer data. The published privacy policy says backup copies roll off within 90 days; actual retention and scheduled jobs must be verified by the CI/CD owner.

Restore must preserve registration tokens, webhook URLs and subscription matching, not just counts. Never show their values in a health report. Restore into isolation first with real outbound posting disabled, then follow the reviewed runbook for production changes. Do not roll back to a data snapshot casually: it can undo registrations, deletion and payment transitions since that snapshot. Deleting a registration does not cancel Stripe billing.

## Stripe and secret rotation

The signed receiver is `/stripe-webhook`. It needs the configured signing secret and the public endpoint; it accepts paid `checkout.session.completed` and `customer.subscription.deleted`. Read [reference](reference.md) for signature tolerance, event matching and unsupported billing events. Use the manage page's payment link so `client_reference_id` is attached. A 200 response for an ignored event is not proof a registration activated.

Payments, portal URL, Discord OAuth credentials, admin access and optional Google/analytics settings are separate configuration entries. Use approved secret storage and trusted consumers, never raw values in argv, public logs, screenshots or docs. Rotation requires updating the provider and the consuming environment, restarting safely, then verifying the relevant function and protecting rollback material. The exact ordered host/provider actions belong to the canonical release/deploy runbook; no guessed secret-rotation shell command is supplied here.

A user can replace a leaked Discord webhook in their own channel. A new webhook changes registration identity; update/migrate through an authorized path and confirm billing continuity, rather than promising the old paid plan follows automatically. See the [user guide](user-guide.md) for ordinary correction, cancellation and privacy.

## Escalation

If health works but connection status does not, start with provider LIVE availability, the actual account handle, webhook validity, signing-service limits and payment state. The shared TikTok retry loop backs off to 30 minutes after five missing-user failures. Do not restart repeatedly just to defeat it. Repeated Discord failures are dropped posts, not a queue that a restart will flush.

Report sanitized status, affected component, first observed failure and deployment revision when known. Do not dump the data volume or admin user table. Use the canonical rollback procedure only after deciding whether image failure or data corruption is the actual cause.

Related: [development](development.md), [architecture](architecture.md), [reference](reference.md), [README](../README.md).

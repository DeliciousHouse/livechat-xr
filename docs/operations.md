# How to operate the hosted relay

Use this index for diagnosis and the CI/CD-owned runbook for procedures that change the service. Never put secret values or customer records into a public runbook.

## Service map

The assigned deployment is Docker container `livechat-xr-relay` on Dora (`node@ubuntu-docker`), port 13300 behind Caddy for `https://livechat.deliciouswines.org`. Persistent volume is `livechat-xr-data`, mounted at `/data`. Runtime secrets are kept on Dora in `~/.livechat-xr.env`, not in the repo or GitHub Actions. This map comes from the deployment brief; it is not a claim that this docs run inspected the host.

The [Dockerfile](../server/Dockerfile) includes `chat.py`, `server.py`, `network.py` and the stdlib `backup.py` helper. It installs TikTokLive, PyNaCl and pinned IANA timezone data (for Pacific DST, including Windows tests), and runs as `nobody`. The data mount must be writable by that user. One process owns the JSON and discovery SQLite volume; do not scale replicas against it.

## Release and deploy owner

CI/CD card `t_7fe554c8` owns the [canonical release/deploy runbook](deployment.md). Use that existing runbook for release, deployment and rollback procedures; this index does not duplicate its commands. Source delivery is not proof of live deployment, backup/restore or secret-rotation acceptance; verify the operational evidence separately.

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

`registrations.json` includes private manage tokens, webhooks, names/emails, stream channels, plans and subscription IDs. `stats.json` persists periodically; logs rotate. `last_backup` and `last_offsite` are epoch-time stamps written by external backup jobs, not backup jobs inside `server.py`. Keep backups protected as credentials and customer data. The prior relay privacy policy stated that backup copies roll off within 90 days. The current policy distinguishes active-record deletion from historic server archives, offsite copies and controller rollback archives; complete archive-erasure timing has not been verified. The CI/CD owner must verify actual retention and scheduled jobs.

Restore must preserve registration tokens, webhook URLs and subscription matching, not just counts. Never show their values in a health report. Restore into isolation first with real outbound posting disabled, then follow the reviewed runbook for production changes. Do not roll back to a data snapshot casually: it can undo registrations, deletion and payment transitions since that snapshot. Deleting a registration does not cancel Stripe billing.

## Discovery incidents

The single top-of-admin panel reports chat loop health independently of discovery.
Missing required setting **names**, never values, appear first with Dora's
`~/.livechat-xr.env` restart guidance. Each Twitch/YouTube source separately enters
delayed state after 15 minutes without success during 18:00–23:00
`America/Los_Angeles`; startup with no polls is timed from network start. One
founder DM is attempted on entry, two consecutive successful polls close it and
attempt one recovery DM. A failed notification is logged without reopening or
spamming the same incident. Healthy sources remain independent; data older than
30 minutes cannot appear in discovery. Persisted closed-DM counts are separate
rows. The same `Network.snapshot()` fields feed later surfaces; no second timer
or duplicated banner state is needed. This cannot alert when Dora/the whole
relay is down; external uptime detection stays deferred.

## Discovery backup and restore

Repository delivery only: Ops did not install this script, execute a live backup,
or migrate production. Dora supplied its installed script read-only: full-volume
Docker copy/gzip, daily `livechat-xr-data-YYYY-MM-DD.tar.gz`, 30-day retention and
`last_backup` after success. That is supplied evidence, not independently verified
installed behavior or schedule. Existing lane E owns installation and live proof.

The reviewed replacement is [server/backup.sh](../server/backup.sh), with
[backup.py](../server/backup.py) installed beside it **and in the reviewed image**.
It preserves unrelated JSON/log files, filename/retention and success stamps,
but replaces raw `docker cp /data` with `python /srv/backup.py archive /data`.
That command takes a temporary online SQLite copy, selects only durable listing
state and explicitly supplied handles, removes platform tables, VACUUMs, validates
integrity/foreign keys and atomically publishes `listings.snapshot.db`. Default
collector-only listing shells are excluded, never classified by `discord_id`.
Raw DB/WAL/SHM, old snapshots and unfinished copies are excluded from the tar.
No network DB means a valid chat-only archive, explicitly marked without a snapshot.

The shell serializes runs, checks the actual compressed archive with
`python3 backup.py check-archive <archive>`, and only stamps after validation,
publication and retention cleanup succeed. A failed run writes `FAILED` and
cannot accept a previous snapshot or advance the success stamp. A pre-existing
same-day valid archive can remain, but it is not evidence that the failed run
succeeded. A stale `.listings-backup-lock` fails closed: investigate interrupted
work before removing the lock. No backup command prints rows or secret values.

Isolated restore procedure (Dora owns any later authorized live procedure):

1. Stop the target relay and disable outbound posting. Use an isolated data volume
   first. Validate the archive with `python3 server/backup.py check-archive <archive>`.
2. Extract the validated data into that isolated directory. The only listings
   restore authority is `listings.snapshot.db`, never a raw live DB from older
   archives. Existing JSON restore rules still apply.
3. Run `python3 server/backup.py restore <data-directory> --relay-stopped`.
   The flag is an operator assertion, not process detection. The helper validates,
   expires overdue removals, removes obsolete WAL/SHM/journals and atomically
   promotes the snapshot. Do not run against a live SQLite writer.
4. Start the actual `Network` with isolated fake transports first. Listings retain
   consent/review/removal and supplied handles but **no resolved platform bindings**.
   Rediscovery creates pending/unconsented shells, never reactivates old listings.
   Fresh authenticated ownership proof and explicit rebinding are required before
   listing, matching or notification. Tests exercise this with reassigned handles.
5. Record image/commit and restore evidence without recording private row values.

Retention limits: removal clears active consent/platform data/peaks immediately;
active removed records and supplied handles expire at seven days, enforced on
startup, maintenance, snapshot and restore. Legacy timestamp-less removals purge
conservatively. Re-opt-in before expiry returns to pending review. **Thirty-day
historic archives are not seven-day physical erasure.** This work does not rewrite
old archives/offsite copies. The prior 90-day statement for unrelated relay JSON
is historical, not a verified archive-erasure deadline. The unchanged deploy
controller also has its own paused rollback tar; this
PR does not claim that path is sanitized. E/C must inventory/verify those copies
before stronger deletion or DR8-wide production policy claims. Restore-time expiry
prevents an overdue record from being served, not its bytes from existing in an
old archive. No new public policy promise, deployment or activation is made here.

## Stripe and secret rotation

The signed receiver is `/stripe-webhook`. It needs the configured signing secret and the public endpoint; it accepts paid `checkout.session.completed` and `customer.subscription.deleted`. Read [reference](reference.md) for signature tolerance, event matching and unsupported billing events. Use the manage page's payment link so `client_reference_id` is attached. A 200 response for an ignored event is not proof a registration activated.

Payments, portal URL, Discord OAuth credentials, admin access and optional analytics settings are separate configuration entries. Use approved secret storage and trusted consumers, never raw values in argv, public logs, screenshots or docs. Rotation requires updating the provider and the consuming environment, restarting safely, then verifying the relevant function and protecting rollback material. The exact ordered host/provider actions belong to the canonical release/deploy runbook; no guessed secret-rotation shell command is supplied here.

A user can replace a leaked Discord webhook in their own channel. A new webhook changes registration identity; update/migrate through an authorized path and confirm billing continuity, rather than promising the old paid plan follows automatically. See the [user guide](user-guide.md) for ordinary correction, cancellation and privacy.

## Escalation

If health works but connection status does not, start with provider LIVE availability, the actual account handle, webhook validity, signing-service limits and payment state. The shared TikTok retry loop retries a missing user every minute, because TikTok also reports "not found" for accounts that have never gone LIVE. Handles are lowercased before lookup; TikTok's LIVE lookup does not match mixed case. Discord chat posts use an ordered in-memory queue with automatic retries and a 25-attempt/minute budget. Only unsent backlog older than 60 seconds is collapsed; `dropped_backlog` counts its original lines. Restarting does not flush or preserve that queue.

Report sanitized status, affected component, first observed failure and deployment revision when known. Do not dump the data volume or admin user table. Use the canonical rollback procedure only after deciding whether image failure or data corruption is the actual cause.

Related: [development](development.md), [architecture](architecture.md), [reference](reference.md), [README](../README.md).

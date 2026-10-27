# Inspector rebind: source-only outer transaction

**Do not run this on Dora as part of this source card, including after merge.**
This is a bounded operator command, not an installer, timer, replacement deployment
controller, or permission grant. E/Dora owns runtime work. Canonical review alone
owns merging the source. No unit, timer, CI policy, runtime source or dependency is
changed by this PR.

## Reconciliation changes the execution preconditions

The original preparation tuple below is historical, **not current host authority**.
During implementation, dev-lead supplied reconciliation 11769: a separate F1 Dora
request `task-de941d63b15047de` / `contextctx-955a32a3ee0a4015` led to execution
handle `inspection-rebind-20261027T171124Z`. Dora's E report says the viewer and
exporter now match the requested revision/digest. This worker read the E report,
not the host, its private files or the actual execution transcript.

Consequently the original canonical-container CAS is stale. This command must
**refuse** that reported current state, not re-create the incident or replay the
old target. It does not adopt a different current container automatically. Any
future live use needs fresh exact identities/CAS, current private snapshots,
applicable action authority and corrected-image rollback. A changed tuple requires
an explicit source update, not editing a retained bindings file to defeat checks.

Reported dependency copying, recursive `chmod a+rX` after a permission failure,
and a live socket rename test are **not** this wrapper's operations. Their specific
original approval/provenance is unresolved in the supplied reconciliation; this
source card does not retroactively authorize, reproduce or repair those effects.
HOLD11750, F1 schedules, Google correspondence and public-activation holds remain
unchanged. Do not wake, reschedule or self-heal F1. A public HTTP 200 empty view is
not populated-cohort, external live QA, provider-health or Google acceptance.

Read-only source evidence `rebind-startup-effects.json` was supplied by PM and read
during implementation. `server/server.py` starts discovery, command registration,
existing registration relays and identity enrichment; `Network.start()` schedules
ordinary poll loops immediately. **A same-image relay restart can cause ordinary
provider requests. No manual forced query is not a zero-provider-call claim.**
Recovery may also start the corrected old relay. No such startup occurred in this
worker's offline fixtures.

## Bounded historical contract

| Field | Exact value |
| --- | --- |
| Source revision | `8df72f06e03e7ee80669f6ebe1e38c5fc7b87309` |
| Five-file source digest | `67c0df0523e296c2dbc80163c7691b4fd4236386c5e74b2b29bf4d86f6e4fcd8` |
| State and native lock domain | `/home/node/.local/share/livechat-xr-deploy` and its existing `lock` inode |
| New source stage | `/opt/livechat-inspection/8df72f06e03e7ee80669f6ebe1e38c5fc7b87309` |
| Original corrected rollback container (STALE CAS) | `139ae8ea5618791ac28e1f60284f6d00690ca30a42f7e324d10a5375ea598562` |
| Corrected image, reused without build | `sha256:b3520b1147b287abd6adb77422c6a94026d0b0da7e20688cd159121c6d263df4` |
| Installed controller | `/home/node/.local/lib/livechat-xr-deploy/deploy.py` |
| Installed controller raw SHA256 | `4a4b46351d6d98a81401ca9d61d776f185c88fadb8e134943f093ff8b9e4b228` |
| Existing interpreter | `/opt/livechat-inspection/82de651bbc1a4f74dc4dc840fd29e081a9174f89/.venv/bin/python` |
| Existing dependencies, never installed/copied here | PyNaCl 1.6.2, cffi 2.1.1, pycparser 3.12 |
| Historical viewer source binding | `82de651bbc1a4f74dc4dc840fd29e081a9174f89` (source path, **not** rollback image/container) |
| Expected stopped name collision | `livechat-xr-relay-previous-8df72f06e03e`, full ID freshly captured, prefix `9655caef36a2` |

Controller custody: the supplied installed hash equals the repository controller
encoded with CRLF (244 lines); the Git LF blob hash is
`561edaccf0fed4d7516304248022ce3ba631ac80e7b840ec8da82d8777581025`.
This local comparison is not independent verification of installed bytes.
Before **every** invocation, the wrapper hashes the actual installed bytes,
compares the AST/signatures of the eight native engine functions against the
reviewed source and compares its probe bytes. A mismatch refuses before runtime
mutation. It never replaces the controller. The expected unit fragment is
`/etc/systemd/system/livechat-xr-inspector.service`; a different effective fragment
or any drop-in is an explicit refusal, not a guessed migration.

## Future command sequence — not executed by this card

Only an already-authorized operator on the correct host may consider these
commands, after reconciling the stale tuple. They intentionally have no force,
ignore-drift, install, privilege-escalation or retry flag. Run from the reviewed
wrapper checkout, **not** the historical viewer stage. `WRAPPER_CHECKOUT` identifies
that checkout; it must include `server/deploy.py` and `server/deploy_probe.py` for
source comparison. `EXISTING_INSPECTOR_ORIGIN` is the existing Q-owned HTTPS
origin, obtained from its actual route custody, not an invented hostname or new
route. Only fixed inspection paths are requested; no redirects, proxy environment,
credentials, arbitrary URLs or provider probes are used.

```sh
cd "$WRAPPER_CHECKOUT"
PY=/opt/livechat-inspection/82de651bbc1a4f74dc4dc840fd29e081a9174f89/.venv/bin/python
BINDINGS=/home/node/.local/share/livechat-xr-deploy/inspector-rebind-bindings.json
: "${EXISTING_INSPECTOR_ORIGIN:?existing Q-owned HTTPS origin required}"

# Observation only: hashes/metadata, full current identities and policy fingerprints.
# Creates one owner-only private observation file; refuses to overwrite it.
"$PY" -B tools/inspector_rebind.py capture --bindings "$BINDINGS" \
  --proxy-origin "$EXISTING_INSPECTOR_ORIGIN"

# Runtime-changing command. Historical/current mismatch must fail BEFORE mutation.
"$PY" -B tools/inspector_rebind.py apply --bindings "$BINDINGS"

# Re-read actual state. An unfinished transaction gives a controlled refusal.
"$PY" -B tools/inspector_rebind.py status --bindings "$BINDINGS"

# After any nonzero/unknown/interrupted APPLY: inspect then recover, NEVER apply again.
"$PY" -B tools/inspector_rebind.py recover --bindings "$BINDINGS"
"$PY" -B tools/inspector_rebind.py recover --bindings "$BINDINGS"  # idempotent verification
"$PY" -B tools/inspector_rebind.py status --bindings "$BINDINGS"
```

Exit 0 means the named operation's checks completed; exit 1 gives only a fixed
non-secret diagnostic. `capture` is not execution authorization. All modes take
exclusive nonblocking `flock` on **the existing native lock**. No second lock,
lock replacement, timer disable, controller-main invocation or remote fetch occurs.

The existing operational principal must already own the private state/env/control
files and be able to atomically replace the unit fragment and stage source and
invoke the existing service controls. The wrapper refuses unwritable directories,
foreign ownership or security drift **before** swapping. It does not grant ACLs,
chmod an installed tree, invoke sudo, alter policy or install a privileged helper.
Actual fragment ownership, host permissions, proxy custody and dependency
availability remain unverified here; a refusal is not permission to change them.

## What apply does, in order

1. Compare current env/unit/controller records (presence, SHA256, modes, owner,
   group, mtime, xattrs), full canonical container/configuration fingerprint,
   exact corrected image, stopped collision identity and effective sandbox
   properties with the captured private observations. Refuse native or outer
   pending transactions. Repeat these comparisons after staging/inspection and
   after capturing a fresh **hashed** continuity baseline.
2. Use **local** `STATE/repo` Git objects only: exact revision, `git archive`,
   every tracked regular file versus its Git blob, no symlinks/submodules or
   untracked files, executable modes and five-file LF-normalized digest. Build
   a new sibling source directory, fsync files/directories, then rename to the
   exact stage. Existing stages must already match bytes/modes; no repair occurs.
   A killed staging operation may leave an unused dot-prefixed sibling; it never
   becomes the live stage or modifies historical source. No venv copy, pip,
   environment/dependency write, network fetch or image build is performed.
3. Persist `STATE/inspector-rebind.json` with fresh current snapshots, baseline
   plus its hash, current rollback identity and unique archival collision name.
   Storage is owner-only (state 0700, journal 0600); fsync the file **and parent**
   before first runtime mutation. Each control input and complete journal is
   capped at 1 MiB; an oversized combined snapshot refuses before mutation.
4. Atomically replace only the existing exact `INSPECTION_REVISION` assignment,
   retaining every other env byte and line ending. Missing, duplicate or
   ambiguous assignments refuse. Preserve mode/owner/group/mtime/xattrs.
5. Inspect the stopped collision by full ID/name, and the absence of its unique
   archival destination, immediately before renaming it. Never delete it.
6. Call the existing `swap(REV, SAME_IMAGE, CURRENT_OLD_CONTAINER, baseline, STATE)`.
   Its commands cross identity guards; its native auto-rollback is retained.
   Temporary chat disconnect is expected. Every baseline registration and
   previously connected registration must recover. No data restore, deletion,
   prune or volume replacement occurs.
7. Validate a fresh response from the **existing** sanitized socket against
   REV/DIGEST and the existing inspection validator. Never force a poll, remove
   or rename a socket, read a raw database or start another exporter.
8. Atomically update only the existing unit's WorkingDirectory/ExecStart source
   binding. All other unit bytes and metadata remain unchanged. Preserve the
   existing approved interpreter and clean-env prefix if present. The exact
   command tail is:

   ```text
   /opt/livechat-inspection/82de651bbc1a4f74dc4dc840fd29e081a9174f89/.venv/bin/python -B server/inspection.py viewer --socket /run/livechat-inspection/projection.sock --port 8787 --revision 8df72f06e03e7ee80669f6ebe1e38c5fc7b87309 --digest 67c0df0523e296c2dbc80163c7691b4fd4236386c5e74b2b29bf4d86f6e4fcd8
   ```

   Then, and only then, future-only `systemctl daemon-reload` and
   `systemctl restart livechat-xr-inspector.service`. No new service/unit/timer.
9. Recheck exact env/unit bytes, effective process argv/cwd/source binding,
   unchanged effective sandbox property fingerprint, current/corrected rollback
   identities, native deployed marker, absent native journal and continuity.
   Check fresh exporter validation and local **and existing external proxy**
   footer/status/availability and negative route/method/query/unknown-Watch
   responses. Compare proxy negative-check fingerprint to its preflight value.
   HTTP 503 remains valid for unavailable data. Only then fsync outer `committed`.

The proxy checks establish unchanged **observable** allowlist checks, not custody
of Q's remote config bytes. Effective systemd property equality is not independent
kernel/BPF/UID/filesystem/proxy security certification. E/Q must supply actual
host/route receipts under their existing lanes; no security acceptance is inferred
from the offline mocks. No populated cohort, provider health, incident cause or
historical/live recovery is inferred from any wrapper result.

## Crash recovery and refusals

The outer journal deliberately survives native `swap()` removing
`transaction.json` and writing native `result.json`. Apply never resumes an
unfinished journal and never repeats a swap after a restart. `recover` reconciles
actual canonical/old/collision identities and known native outcomes first. An
unknown command outcome is inspected; failed Docker inspection is not treated as
proof of absence (successful inventory is required).

Recovery restores **current captured** env/unit, composes native `rollback()` with
the corrected 139ae8/b3520 container, restores the stopped collision's original
name without overwriting a new owner, reloads/restarts the prior viewer, verifies
continuity/bindings and restores deployed/result/failed **presence, exact bytes and
metadata**. It never uses `runtime-env.rollback` or an old 82de651 container/image.
The historical 82de651 *viewer source/interpreter* may remain exactly as captured;
a prior source mismatch truthfully returns 503 on recovery while the corrected
relay/image stays intact. Filesystem inode/ctime are inherently changed by atomic
replacement; preserved metadata means mode, uid, gid, mtime and xattrs, not invented
ctime/inode identity.

Interruptions during native rollback, collision rename, service controls or
record restoration can be recovered repeatedly. Already-native-recovered OLD is
recognized; only this transaction's exactly bound candidate can be removed by the
native engine, without a volume-delete option. Native failed-marker side effects
are replaced with the pre-transaction marker bytes or absence. A completed outer
commit is not automatically undone: `recover` refuses it; `status` revalidates it.
After commit, rollback is a separate operator disposition, not blind replay.

Both explicit recovery and native auto-rollback route candidate removal through
the same guard. After validating the candidate identity, it requests a bounded
30-second Docker stop so the relay can close its inspector and unlink only its
owned socket inode. It reinspects the full candidate identity and requires a
stopped, unpaused container with exit code zero before a **non-forced** removal.
A stop error, forced kill, failed cleanup or changed identity leaves recovery
unfinished: retain the candidate and journals and inspect before further action.
There is no forced-delete fallback or host-side socket unlink. A replacement
socket inode remains untouched; a failed restored bind must not count as recovery.

If the timer wins the native lock after a crash, or another operator changes
identities/config/controller records, recovery refuses unknown outcomes rather
than overwriting a new owner. In particular the unchanged timer cannot understand
the outer journal: it may perform native recovery or advance its marker while
this process is absent. The wrapper accepts only known native/outer progress; it
does not pretend to freeze future timer activity. Keep the same E operator lane,
inspect current state, and route an identity-drift diagnostic there. Never delete
journals/markers, stop the timer, change permissions, use stale snapshots, or
blindly retry to make a refusal disappear. Retain private evidence locally; never
paste env/unit bytes, projection/page bodies, registration hashes or journals
into stdout, tickets, PRs or receipts.

## Offline verification

From the source checkout, using its existing Python environment:

```sh
env -u PYTHONPATH python -B -m unittest discover -s tests -p test_inspector_rebind.py -v
env -u PYTHONPATH python -B -m unittest discover -s tests -p test_deploy.py -v
env -u PYTHONPATH python -B -m unittest discover -s tests -p test_inspection.py -v
```

The matrix invokes the actual command entry point and native swap/rollback with
only disposable files and mocked Docker/systemd/probe/HTTP boundaries. It covers
before/after outer durability phases, command/native stamp boundaries, interruption
during recovery, native auto-rollback, native-journal-cleared acceptance failure,
marker absence, exact bytes/metadata, staging/drift, collision/new owners,
controller hash versus reviewed source, process/proxy binding and truthful 503.
The deployment suite also runs real disposable Unix exporter processes through
both rollback paths, including forced-stop and cleanup failures, candidate
identity drift and a foreign replacement socket inode. These lifecycle cases and
native flock/xattr checks run on Linux; Windows explicitly skips them and mocks
only the lock in transaction fixtures. Existing tests and CI policy are
not weakened. Offline success is **not** Dora execution or real security/cohort
acceptance. Source rollback is a normal reviewed revert of the wrapper, tests and runbook.

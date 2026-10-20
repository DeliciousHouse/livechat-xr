# Bounded synthetic capture: source package contract

Status: SOURCE IMPLEMENTED, RUNTIME NOT PROVISIONED OR EXECUTED. Source tests mock
fixture, process launch, namespace and process-inspection boundaries. Their PNG
bytes are unit-test data, not captures. No browser, bwrap, acquired binary,
installation, policy read, provider or private state is exercised by these tests.

Task: t_f663a921; readiness: t_5d402dde. Supplied contract SHA256:
`59dc444583847d9ab764270fb2016dd3b93c7200db26e8b598fb65cb64964b40`.
Dev-lead must review this source/plan contract. The native poller alone creates
canonical review; its assigned reviewer alone controls merge. Source delivery
preserves PR46's completed lanes, F NOT ACCEPTED/Eparent, F1 October 21
23:07:47 UTC, G and all identity/title/device gates. No owner notice is implied.

## Source check and package

Run only the mocked source check under this card's authorization:

```
python -B -m unittest discover -s tests -p test_qa_capture.py -v
```

The Linux CI lane exercises POSIX exclusive/no-follow file checks and mocked
PID cleanup as well. Windows skips those POSIX-specific tests; all other boundary
checks run there too. These tests need only the standard library and never import
`qa_relay`, `server`, `chat` or their dependencies. The fixture's scenario tuple is
checked against its AST, not by importing or starting it.

Package the files in `tools/qa-network-manifest.json` at the reviewed commit,
preserving relative paths, with the existing per-file source SHA256 receipt.
The manifest includes the wrapper, this contract and its focused safety test.
This is source, not a runnable runtime archive or an authorization to acquire,
install, materialize or execute its dependencies.

## Fixed future layout and entrypoints

`R = /home/node/hermes-dora/qa/vrln-viewonly-5d402dde`

`O = /home/node/hermes-dora/evidence/vrln-pr46-readiness-20261020/synthetic-captures`

Do not create either path under this source-only grant. Following separate
acquisition/materialization and exact execution disposition, the reviewed
`tools/qa_capture.py` bytes would be copied to `R/launch-viewonly` without edits.
All of `R/python`, `R/venv`, `R/source`, `R/browser` and generated `R/config`
would be staged at their final absolute paths, never relocated after venv creation.
No existing-cache browser is endorsed; Q's official CfT 156.0.8078.4 provenance
selection remains separate. Python/wheel/native closure and effective sandbox
behavior remain unestablished.

The outer wrapper's proposed interface is `--plan <absolute-json-file>
--plan-sha256 <reviewed-sha256> --scenario normal`. Its own interpreter must be the
same approved QA interpreter, invoked with `-I -B`. Inside bwrap the fixed argv is
`R/venv/bin/python -I -B R/launch-viewonly --inside normal`. The internal switch is
not a production launcher flag. Before output or fixture work, it refuses outside
Linux namespace UID 1000 / PID 1, without
isolated/no-user-site/no-bytecode flags, with an unexpected environment, or if any
of pid/net/mnt/user namespace IDs equal the creation receipt's parent IDs.

The added bwrap `--as-pid-1` makes the wrapper the namespace orphan reaper;
`--unshare-all --die-with-parent --new-session --clearenv --uid 1000 --gid 1000
--cap-drop ALL` remain mandatory. No share-net, debugging, no-sandbox, host device,
credential or host interpreter fallback exists. Kernel/library/browser failure
is failure, never a request to relax sandboxing. No code reads
`/sys/kernel/security/apparmor/profiles`; do not retry that denied read by any route.

## Reviewed plan schema (not supplied runtime evidence)

The plan is a bounded JSON object (64 KiB), owned regular non-symlink file at a
canonical absolute path. Its exact SHA256 is supplied separately after review.
Unknown fields and duplicate mount targets refuse. `example_plan()` documents
the schema with inert zero hashes and PENDING references; PENDING refuses runtime
preflight. Filling fields is not permission to execute.

Required fields:

- `version`: integer 1.
- `source_commit`: lowercase 40-hex reviewed source revision.
- `source_receipt`, `execution_disposition`: non-secret receipt/disposition
  references, restricted to 160 ASCII identifier/path characters. They are
  references only, not a cryptographic attestation or a substitute for approval.
- `provenance_sha256`: exact lowercase 64-hex hash of Q/Dora's provenance manifest.
  This is a reference, not an invented upstream browser digest.
- `trees`: exactly `python`, `venv`, `source`, `browser`, each a lowercase SHA256
  of that staged tree using `tree_digest` below.
- `files`: exactly `launch-viewonly`, `config/fonts.conf`,
  `config/nsswitch.conf`, each a file SHA256.
- `resources`: at most 512 `{source, target, sha256}` entries. Each is an
  individually hashed regular file, with canonical real source path and requested
  absolute loader/library/font target under `/lib`, `/lib64`, `/usr/lib`,
  `/usr/lib64` or `/usr/share/fonts`. No directory resource bind is accepted.
  Re-derive this list from acquired bytes; the static candidate resource map is
  not certified closure. No `/etc`, `/usr`, `/home`, `/run`, resolver, profile,
  D-Bus or font-cache directory bind exists. Only the generated QA NSS file is
  separately bound at `/etc/nsswitch.conf`.

A tree digest sorts relative paths and hashes UTF-8 JSON lines containing
`[relative_posix_path, permission_bits, value]`, with compact separators and LF.
For a directory `value` is `dir`; for a file it is its SHA256; for a symlink it is
`link:` plus the exact link text. Links must resolve to regular files within the
separately hashed QA Python/venv prefixes; directory symlinks and escaping links
refuse. Every QA tree entry must be owned by the invoking UID. Bounds are 100,000
entries, 512 MiB per file and 2 GiB file content per tree; they bound inspection,
not runtime memory/CPU/filesystem use. The outer lifetime also bounds inspection.

`pyvenv.cfg` must specify `home = R/python/bin`,
`executable = R/python/bin/python3.12`, and
`include-system-site-packages = false`. Venv script shebangs must name
`R/venv/bin/python` exactly; venv Python resolves within QA prefixes. Installed
runtime bytes, native loader and shebangs must be verified before any invocation.

Outer preflight requires fixed Linux host UID 1001 (`HOST_UID`), not an arbitrary
non-root actor or caller-selected UID. Host UID 0, 1000 and all other actors refuse
before plan inspection. Inside bwrap, the fixed `--uid 1000 --gid 1000` mapping is
unchanged and inner entry explicitly requires namespace-local UID 1000.
Ownership checks compare to `os.getuid()` in their current namespace: host 1001
outside, mapped 1000 inside. Shared loader/library/font resource files retain
their existing ownership exception; no ownership rewrite or account switch occurs.
Preflight validates hashes and layout, and checks the
materialized launch file is byte-identical to the reviewed wrapper being run.
The plan is trusted reviewed input, not a service accepting arbitrary users'
mount requests. Freeze staged input bytes against concurrent modification for
any separately approved run; hash validation is not a filesystem snapshot.

## Capture and lifetime

The reviewed fixture API is `fixture(port=0, network_fixture=True,
scenario=scenario)`; only `normal`, `delayed`, `closed-dm` are accepted, default
`normal`. Binding must be literal `127.0.0.1`, port must be an actual integer
1..65535 (not bool/string). There is one fixture and one Chromium invocation.
No Network.start, production main, runtime scenario mutations or providers.

Chromium argv is fixed: `R/browser/chrome --headless
--user-data-dir=/qa/browser-profile --no-first-run --disable-background-networking
--disable-sync --screenshot=/qa/capture/home.png http://127.0.0.1:<port>/`.
Only the home capture is requested. The existing GET ceiling is unchanged;
no manage/admin tokens or keys are guessed and no request bodies are sent.

The environment is replaced, never merged: HOME `/qa/home`, TMPDIR `/qa/tmp`,
PATH `R/venv/bin`, LANG `C.UTF-8`, FONTCONFIG_FILE `R/config/fonts.conf`. These
names, not inherited values, enter receipts. Home/profile/capture/temp are new
internal tmpfs directories. Standard input is DEVNULL, stdout/stderr are pipes,
close_fds is true, shell is false, no detached launch flags.

Bounds: fixture startup/import 15 seconds, browser 20, inner total 45, independent
outer child budget 45, outer invocation 60 (including preflight). Each pipe is
drained and discarded, capped at 64 KiB; exceeding either fails. Context exit has
5 seconds. Owned process cleanup sends TERM with a 5-second join bound, then KILL
with another 5-second join bound. Process identities use PID plus Linux starttime
and pidfds; only recorded owned descendants are signalled/reaped. Discovery has
a 512-task inspection ceiling, not an OS task quota. Reused PID identity refuses
signalling. Cancellation still goes through finalization; no detached tasks or
cancel-and-forget are used. A post-launch setup/inspection failure is fatal: no
pidfd retry or capture follows. The sole synchronous Popen owner still terminates
and waits for its direct, unreaped child (TERM/5 seconds, then KILL/5 seconds);
that unreaped child cannot have a reused PID. This fallback never targets
discovered descendants. Receipts distinguish `direct_child_joined` from `joined`:
without complete descendant evidence `joined` stays false, and the invocation
fails even when the direct child was reaped. Output is copied only after browser cleanup and
fixture listener/loop shutdown evidence.

The parent deadline may terminate the sandbox when ordinary cleanup cannot finish.
This is containment, NOT proof of finalizer execution. Missing/failed child cleanup
is failure and needs separately scoped reconciliation. SIGKILL/host crash cannot
produce a guaranteed final receipt. No global kill, arbitrary cleanup, cache
pruning, host network/security changes or disk-data deletion is authorized.

## Output and receipts

O's canonical, owned parent must already exist under future authorization. O is
created exclusively mode 0700; any pre-existence (even empty/owned), symlink or
foreign owner refuses, with no reuse/overwrite/delete. Only O is writable-bound;
input trees/files/resources and plan/creation receipts are read-only mounts.

The browser writes internal `/qa/capture/home.png`, not O. After child exit,
no-follow/nonblocking open and fstat require an owned regular file of at most
10 MiB. PNG signature, chunk framing/CRCs, single IHDR, nonzero dimensions up to
1920x1080, IDAT and final IEND are checked. This is structural validation, not
pixel decoding or UI acceptance. Bytes are copied to exclusively created
`O/home.png` mode 0600 using directory-fd relative O_EXCL|O_NOFOLLOW opens.

`creation.json`, `receipt.json` (inner) and `parent.json` are each exclusive,
mode 0600 and bounded to 64 KiB. No stream bodies/raw HTML/tokens/profiles or
exception text is retained. The maximum wrapper-retained output is one 10 MiB
PNG plus three 64 KiB receipts, below 12 MiB. A partial output is not success and
is preserved for separately authorized reconciliation, never silently removed.

Creation records task/source/disposition references, plan/provenance hashes,
R/O, ownership, parent PID/start/namespaces, exact sandbox/mount argv and environment
key names. Inner records scenario/actual synthetic port, wrapper and browser
identities/namespaces, browser exit/timeout/join and descendant records, fixture
thread/listener closure, timestamps and actual PNG path/hash/size/dimensions.
Parent records independently observed sandbox exit/cleanup and capture hash
readback. Its failure receipt survives an inner failure where the parent can
still finalize; neither missing PNG nor missing receipt is synthesized as success.

Existing `uid` fields are namespace-local: creation records host UID 1001 and the
inner receipt records UID 1000. No receipt schema or runtime API is added by the
actor compatibility correction (t_c99a32e1, dev-lead contract comment 9892).

Namespace ID comparison and process joins are observations, not blanket isolation
PASS. Browser sandbox effectiveness, effective loaded policy, hard memory/CPU/
filesystem/task quotas and tmpfs disappearance are UNVERIFIED/UNESTABLISHED.
Walltime/output limits do not replace those missing controls. Future Dora-owned
runtime/resource and exact cleanup receipts remain required before acceptance.

## Review handoff

Dev-lead must disposition the schema, `--as-pid-1` orphan-reaper addition,
read-only plan/creation mounts and two-level lifetime behavior against the supplied
contract. The official archive/acquisition request is still separate and inert;
source delivery does not authorize acquisition, venv creation, installs, browser,
namespace or capture execution, security changes, deployment or private QA.
No title/identity/device/transport acceptance is inferred and F remains unaccepted.

# Synthetic view-only network fixture: implementation evidence

Card: t_9d60a336. Goal: G-02. Base: afaf2aff4ea47a0e9bc23a5b4ee47ea068a85257.

This is offline source/test evidence only, not live QA, F acceptance, provider
behavior, identity/channel-control, Discord delivery or Quest proof. No runtime
was provisioned, production state used, or deployment performed.

## Acceptance

- Explicit `--network-fixture` with fixed normal/delayed/closed-dm startup choices;
  default relay-only registration/test/delete/reset/stop journey still passes.
- Initialized disposable SQLite with one directly seeded synthetic linked/reviewed
  listing/channel association; manage listing and admin totals render real templates.
  Literal empty Network config; Network.start is forbidden by the safety test.
- Exact GET allowlist only. Unauthorized GETs and non-GET verbs are rejected before
  production dispatch (tests replace production dispatch with raising sentinels).
  Both callbacks, registration/recovery, Watch, interactions, Stripe, runtime
  reset/stop and management mutations are unavailable.
- No usable forms, external hrefs, scripts, font/preconnect links or redirects in
  allowed HTML. Controls disabled both natively and visually. Restrictive CSP.
  Synthetic labels and status wording distinguish simulated proof/listing state
  from actual activity. OAuth/Google/GA/billing/donation config blanked.
- urllib urlopen and constructed opener egress fail closed. Fake Discord result
  is local only. No collectors, provider calls or external identity flow.
- Owned HTTP and loop threads joined; listener and loop closed; SQLite connections
  scoped to operations; temporary directory removed; adapters/globals restored
  after normal and exceptional context exit. Listing state unchanged by requests.
- QA source allowlist/dependencies in tools/qa-network-manifest.json; runbook and
  bind/isolation contract in docs/qa-fixtures.md. IPv4 127.0.0.1 only, no new bind
  option and no container-publishing assumption. Separate QA zip, not production image.

## Execution

Python 3.12.12, Windows. Dedicated local venv with PyNaCl 1.6.0 and tzdata 2026.5.

`python -m unittest tests.test_qa_network_fixture tests.test_qa_fixtures.RelayFixture tests.test_network_web -v`

Result: 12 tests passed. The new single safety check traverses all three scenarios
and exceptional cleanup. Initial red: missing `network_fixture` argument; green
following implementation. A second red/green check guards the visual-review fixes
(disabled styling and removal of misleading collector/restart status copy).
The existing lookup-error journey intentionally emits `channel check: TimeoutError`.

`npm run verify`: unavailable, Missing script: "verify". This is the existing
Python-only repository, not a new failure. `git diff --check`: passed.
Full cross-platform suite and installer/DLL packaging remain CI's responsibility.

## Local source render inspection

Headless Chromium local synthetic source renders only, at 1440x1000 and 390x844,
for home/manage/admin. Browser route guard rejects external destinations; observed
external requests: zero. DOM checks: zero forms and zero enabled form controls.
Playwright 1.58.0 is a verification-only dependency, not part of the QA launcher
runtime. Browser installer timed out; the existing compatible headless Chromium
cache successfully executed the renders instead (no real signed-in browser used).

Vision inspection identified active-looking disabled controls and misleading
collector/proof/admin status wording; launcher-only changes corrected these and
were re-rendered. Second inspection found no clipping or unreadable text. The
suggestion to repeat 'synthetic' beside every count/date was declined: one explicit
page-wide banner governs all sample data, avoiding per-value repeated caveats.
External font removal/system fallback is intentional, not a production redesign.

## Scope/review handoff

Scope Check: CLEAN. Production server/network/backup/chat/Dockerfile unchanged.
Pre-landing critical then informational checklist: no open findings; no existing
tests/gates removed or relaxed. Process-global mock harness is exclusive to this
isolated launcher; it must never be embedded beside production in the same process.

Dev-lead contract review and canonical reviewer approval/merge are outstanding.
Only the existing review poller creates canonical review. Do not merge/deploy from
this evidence. Dora's next receipt is the reviewed exact commit, distinct QA zip
and hash receipt, safety result, manifest and later approved runtime-isolation
mapping. The implementation does not alter E/F/F1/G, identity/device custody,
reserved commitments or scheduled wakeups. No additional review/job/card created.

## Authorized test-clock correction

Dev-lead reviewed the initial launcher/ingress/bind/package contract with no
defect found, then authorized only a shared test-clock correction after exact-head
CI reproduced two failures and one error in `tests/test_network_surfaces.py`.
`Surfaces.asyncSetUp` now pins wall time to its existing `evening()` fixture before
Network creation, initialization and enrollment; unittest restores the clock after
owned async cleanup. Implicit owner-lookup expiry and explicit presence timestamps
now share one clock. No production expiry behavior or assertions changed; nested
wall-clock overrides, explicit staleness/expiry times and monotonic proof expiry
remain exercised.

Local red: the original 17 surface tests reproduced the same two failures/one error.
Green: `python -m unittest tests.test_network_surfaces tests.test_qa_network_fixture
tests.test_qa_fixtures.RelayFixture tests.test_network_web -v` ran 29 tests, all passed.
One initial combined command used the nonexistent selector `Relay`; corrected to
the documented `RelayFixture` before recording this result. `npm run verify` still
reports Missing script: "verify". `git diff --check` passes. Critical then
informational checklist: no open findings; scope is the authorized test clock and
this evidence addendum only. Exact-new-head CI and regenerated package receipt
are separate handoff evidence, not implied by these local results.

## Fixed host / namespace actor correction

Card t_c99a32e1 implements dev-lead contract comment 9892 against main
df63a89b160d8930216083ad0a4ffd21b5518838. Outer preflight pins host UID 1001;
inner entry pins namespace UID 1000 before output/fixture effects. Bubblewrap
UID/GID 1000 and current-namespace ownership comparisons remain unchanged.

Offline red/green: the new checks rejected the old outer UID guard and absence
of an inner UID guard. The focused capture suite now runs 22 tests: Windows
Python 3.14.7 passes with four POSIX skips; WSL Linux passes all 22. Matching
host-owned plan/runtime metadata reaches a deliberate stop before tree inspection,
not runtime readiness. Inner UID 1000 reaches a deliberate stop before fixture
work only with the existing PID/environment/namespace/owned-creation contract.
Foreign input/output/tree ownership, other actors and hash mismatch refuse.
Existing cleanup failure checks remain intact. No real launch, namespace,
browser, fixture, acquired-code execution, ownership rewrite or policy read ran.

The unchanged source manifest already includes all four changed files. Generate
the allowlisted zip and per-file SHA256 receipt from exact committed Git blobs,
not Windows checkout line endings. That receipt is a review candidate until the
existing dev-lead/canonical reviewer workflow approves it; source packaging is
not dependency acquisition, runtime staging, execution approval or F acceptance.
`npm run verify` remains unavailable (Missing script: "verify").

# C1 private fixture evidence

Task: `t_76f5d78f`; outcome owner C: `t_f8b3e2b6`; successor C2: `t_b1e354a7`.

This is repository implementation and isolated fixture evidence, not live Discord,
Quest, source quota, policy clearance, public activation or deployment acceptance.
The exact source SHA and CI run are recorded in the card's completion metadata.
Requirement-to-test and shared schema/API ledger: `docs/network-operations.md`.

## Executed locally

- Python 3.12, pinned app requirements plus PyNaCl1.6.0 and tzdata2026.5.
- `layer/build.cmd`: native layer and runtime-test DLL compiled successfully.
- `python -m unittest discover -s tests -v`: 208 tests OK, one Linux-only nightly-shell
  skip. The original E3/E4 per-line latency/block/crash/missing-setting assertions
  were not changed. Linux shell behavior remains an exact-head CI obligation.
- `git diff --check`: clean. No npm manifest / `npm run verify` in this Python repo.
- The new checks use real SQLite transactions, actual sanitized archive bytes and
  v1/v2 restore; actual Ed25519-signed local HTTP interaction requests; actual local
  `/go` 302/404 requests. Official APIs and Discord REST are synthetic transports.
- Initial new tests failed on missing `owner_listing`/renderer before implementation.
  A later fixture failure exposed an unclosed SQLite connection on Windows; it was
  closed with stdlib `contextlib.closing`, not hidden with a skip or timeout change.

## Scope and review

Scope check: C1 private surfaces/profile schema2/removal/redirect only. No deploy.py,
chat relay, package dependencies, production migration, global command registration,
Discord identity, follow/squad activation, paid checkout or speculative tracker.
The existing Linux CI job now includes the new surface module.

The two-pass checklist examined parameterized SQL/transaction rollback, consent and
owner/founder boundaries, collector/proof/removal ordering, replay/signature guards,
external-field escaping, expiry and off-loop I/O. Fixed before publication: mutual
0–2 matching; first-owner proof/opt-out race (cancel before serialized owner lookup,
recheck before proof commit); successful proof consumption despite a failed welcome;
recipient alert gates rechecked before transport; orderly stop after task join.
A threaded regression pauses proof inside its uncommitted transaction and proves
opt-out cannot resurrect an owner. No E3/E4 guard was weakened.

## Payload preview (not native Discord)

Reproduce with `python -m tests.network_surface_preview evidence/vr-live-c1`.
`index.html` contains real renderer/command output with synthetic streamer fixtures.
Private proof code is omitted; no interaction tokens, management links, real user
rows, email addresses or credentials are exported. Screenshots are generated browser
captures of this HTML, not generated artwork and not images of Discord or Quest.

- `desktop.png`: width1440, full page.
- `mobile.png`: width390, full page; DOM scroll width390 (no horizontal overflow).
- Both captures were inspected with `vision_analyze`. Desktop: no concrete clipping
  or readability defects. Mobile: text wraps and is somewhat dense; left unchanged
  because this preview must preserve the pinned Discord row/footer copy and does
  not control native Discord layout. Raw timestamp tokens are intentionally shown;
  enabled-source labels can name YouTube when the sample has only live Twitch rows.
  Those are not claims of native rendering or YouTube sample live presence.
- Impeccable context launcher could not download engine0.1.6; no detector result is
  claimed. Existing DESIGN.md and the skill's craft floor were read directly. No
  product CSS or visual-system files changed.

## Deliberate limits / handoff

- Physical Quest DM settings path and actual notifications remain unverified.
- E6 unique-visitor measurement is UNAVAILABLE: no cookies, visitor IDs, IP/UA data,
  click counter, redirect log trail, tracking table or threshold-passed claim.
- Outside-five demand cohort is unknown; `Store.demand().outside_cohort` is None.
- Staged ping/follow/squad wording is fixture-only, never posted or activated.
- Draft PR is an implementation-phase handoff. C2 must wait for the sole canonical
  review and verified main ancestry. C remains gated until merged surface acceptance.
- Rollback: reviewed revert/config-off preserving chat, no force-push, no old-writer
  schema downgrade, no restoration of expired/deleted platform data.

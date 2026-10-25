# F1 implementation receipts — 2026-10-25

Code revision: `b083eccae7aaa9b2aaa0017c47ccc99ff6a1d756`.
Source digest: `33f61ecc92c0f0f6100e451ec3e69b9a30e50b41fe69388356bbd3040b684e89`.
See `artifact-receipt.json` for immutable source-bundle SHA256/bytes and an actual isolated-extraction CLI execution. That command used a nonexistent disposable cache: availability unavailable, aggregate null, no cache created. It is NOT production-empty evidence. Source bundle preserves only the seven named code/test/dependency files from this commit, with LF bytes; not a production DB or runtime export.

## Executed tests

Windows Python 3.12: `python -m unittest tests.test_network tests.test_network_operations tests.test_network_surfaces tests.test_network_backup tests.test_network_web tests.test_server tests.test_qa_network_fixture tests.test_qa_capture tests.test_discord_delivery tests.test_inspection -v`: 159 tests run, 6 platform-specific skips, exit 0. Full native output: `scoped-tests.txt`.
WSL Ubuntu Python 3.12: `python -m unittest tests.test_inspection -v`: all 16 inspector tests passed, exit 0, including real Unix-socket IPC and cleanup. Native output: `linux-inspector-tests.txt`. This is non-Git test execution; no Windows Git variables or Git commands were passed into WSL.
No package.json/Node suite: npm verification not applicable. Docker/Windows packaging and exact-head required gates are CI-owned, not claimed locally built. The only workflow edit appends the inspector module to the existing Linux discovery/isolation lane. No timeout, assertion, threshold, old test or qa guard was relaxed/removed. New Unix IPC test skips Windows only; it ran on Linux.

## UI / source review

Loaded shipping-things-people-use and the available design skill, followed existing DESIGN.md. Synthetic actual-HTTP captures at 1440x900 and 390x844 cover available, observed-empty and unavailable; response statuses and zero horizontal-overflow measurements are in `visual-receipt.json`. Source IDs are actual code revisions, not production observations. No external page resources were requested.
Vision findings led to explicit cache/not-live labels, distinct cached creator versus audience counts, unknown-count wording, original observations and generation provenance, and clearer source labeling. Deliberately retained Pop1 Live as existing source branding, paired with the cache-not-live disclaimer. Declined a broader card/layout restyle because it would fork the shared renderer and exceed this minimal inspector; retained its slightly dense source/provenance typography and singular/plural count wording as cosmetic limitations. No live-population or external-availability conclusion follows from these fixtures.

Ship checklist: Pass 1 covered data/query boundaries, Store locking/current revalidation, input/escaping/destination trust, method/path guards and CI gate integrity. Pass 2 covered async boundary, field/schema parity, canonical timestamps/unknowns, bounded work and packaging. Fixed missing-observation zero semantics, optional-listener continuity, bridged-container sanitized IPC, and locale-dependent CLI output, with regression tests. Final code tests reran after fixes. Performance/scale and kernel/network sandbox enforcement remain unmeasured until Dora host execution.

## Handoff / limits

`docs/inspection.md` is the versioned field/schema, expiry/withdrawal, export/start, process/bind, GET-only proxy and rollback contract. Dora E t_c16887ba owns host operations; Q owns its narrow proxy/controller and Google/public-activation disposition. Canonical review intake owns the sole review identity and merge; dev-eng did not deploy or self-merge. Required exact-head CI/reviewer receipts are delivered on the card after publication, not invented here.

Oct24 evidence/hashes were not edited. No real cache/private profile/credential was dumped; no provider polling, public activation, email/upload or production host action occurred. Fresh anonymous external inspection and a lawful populated-live cohort are independently NOT DELIVERED. True observed-empty is permitted only from lawful actual runtime observations; unavailable is not zero.

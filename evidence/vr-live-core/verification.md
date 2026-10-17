# Core implementation verification

Task t_9ab0ea99; T11/T12/T17 and strict DR8. Synthetic local data only.

- Windows Python 3.12.12, built native layer: `python -m unittest discover -s tests -v`: 191 tests, OK, 1 Linux-only shell test skipped (55.018s). E3/E4 assertions unchanged. Non-fatal unittest teardown ResourceWarning for an unclosed ProactorEventLoop remains; not presented as a regression fix.
- Linux scoped network/operations/backup/Discord delivery: 45 tests, OK (17.129s); final operations/backup rerun after healthy-summary freshness assertion: 17 tests, OK (2.931s). Real SQLite backup/VACUUM, tar/gzip/archive validation and restored Network startup exercised; Docker transport is a fixture, not a deployed Docker/host test.
- `layer/build.cmd`: built successfully; Windows layer tests exercised in full run.
- `npm run verify`: unavailable (Missing script: verify); repository uses unittest, no package.json.
- Actual HTTP admin fixture, desktop 1440x1000 and mobile 390x844: screenshots alongside this note. Vision review confirms readable chat-running/source-stale distinction and wrapping without panel clipping. Warning icon spacing corrected. Intentional warning border retained; chat status is independently explicit. Legacy signup table remains horizontally crowded on mobile, outside this incident-panel scope; surfaces lane can refine the whole admin layout.
- Manual diff/checklist review: SQL parameters and transactions; allowlisted schema/payload sanitization; no restored collector binding; archive failure/stamp paths; async worker lifetime; escaping/admin authentication; CI dependencies and Docker COPY reviewed. Duplicate verified binding rejected; stale polls cannot claim healthy summary outside alert window; health version includes backup module. Regression tests cover conflicts and FK rejection. No change to chat.py, deploy.py, or E3/E4 assertions.
- Scope: one runtime, existing data volume and maintenance loop. No new service/scheduler, public opt-in/list/redirect, visitor measurements or payments.

This is repository implementation evidence, not host installation, production backup/restore, migration, deployment, public activation, or F1/O6/terms acceptance. Historic 30-day/offsite/raw rollback archives do not establish seven-day physical erasure. See docs/network-operations.md for exact API/provenance/retention and runnable requirement ledger. Exact-head GitHub CI and canonical reviewer disposition are recorded on the card/PR, not guessed here.

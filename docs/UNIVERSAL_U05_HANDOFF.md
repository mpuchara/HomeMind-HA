# Universal U05 handoff (2026-11-02)

## Checkout, dependency chain and exact reference

Worktree:
`C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`

Current development branch: `codex/universal-u05a-training-data`.
Last implementation commit: `5643a9247056ad53dac778c7dcc71fec3d0bc842`.
Documentation-only validation commits can follow it; use `git log -5 --oneline`
to identify the checkout's final HEAD without confusing it with a code change.

Requested production reference: **0.14.232**, exact commit
`f7384ba5476f26155ecb138034cc3350fa5bfda2` (open Draft #348 at audit time).
Actual GitHub main was still `91c7343324d38a1922bfed6486b4ef6dd7a3ba06`
(0.14.184). Do not restart from main or the old U04c branch.

1. [Compatibility PR #355](https://github.com/mpuchara/HomeMind-HA/pull/355),
   branch `codex/universal-232-compat`, tip
   `859c372cf9418cf63dae7063f7ded78369a0dde3`, targets the exact .232 branch.
   Merge commit `2ed401aac8a4f415017b64d1932f8aca61ed524a` has .232 and
   U04c `e2989925573a5bc2748e87652570125232d3f69c` as its two parents.
2. [U05a PR #356](https://github.com/mpuchara/HomeMind-HA/pull/356),
   branch `codex/universal-u05a-training-data`, targets #355's branch.

Both are Draft. No PR was merged; #349-354, main, version and install artifacts
were left unchanged. The user's 0.14.232 reference overrides the old attachment's
0.14.184 instructions. Seventeen key production modules retain exact .232 blobs.

## Completed implementation

* Ported U01-U04c while preserving .232 Engine, composition, startup scopes,
  Correct/Candidate/Shadow, DeviceAgent/Executor, storage and training changes.
* Resolved the important REST conflict by keeping archive persistence on the
  housekeeping worker. Never restore the old inline `flush_archive()` call.
* Added U05a offline sample/receipt/coverage contracts, native journal and
  archive readers, causal replay, trusted provenance, undo-before-dedup,
  root-lineage revocation, immutable manifests and future holdout splitting.
* Added byte/row/aggregate-reference limits and existing training-budget yields.
  Native U02 journal reading gains an optional aggregate byte cap and callback.
* Added 25 U05a tests including native production journal undo and
  5/1, 50/10, 200/30, 1000/200 sensor/device fixtures.
* Hardened bounded replay intervals and dangling feedback links. A missing
  origin cannot conceal the logical identity needed to revoke a child copy.

Contracts and source audit:
`UNIVERSAL_U05A_TRAINING_DATA.md`, `UNIVERSAL_U05_DATA_SOURCE_AUDIT.md`.
Compatibility details and protected blob receipts:
`UNIVERSAL_232_COMPATIBILITY.md`,
`benchmarks/UNIVERSAL_232_COMPATIBILITY.json`.

## Validation receipts

* Before U05a implementation, full integrated local suite: **2903 tests in
  453.717 seconds, OK**. Two new .232 integration tests passed separately;
  the complete universal subset then passed 81 tests.
* Port code commit 2ed401aa:
  [CI 41391490830](https://github.com/mpuchara/HomeMind-HA/actions/runs/41391490830)
  passed full Python 3.11/3.13 tests and image build/smoke. Package was skipped.
* Before the final byte/window/dangling-link hardening, the U05a full local
  suite passed **2929 tests in 390.468 seconds, OK**.
* Latest U05a source: **106 universal tests in 6.656 seconds, OK**.
  The full acceptance run for the exact code commit is
  [CI 41393396527](https://github.com/mpuchara/HomeMind-HA/actions/runs/41393396527).
  Inspect its live result on continuation; an old SHA's green check is not a
  substitute. A documentation-only HEAD may have its own later PR check run.
* A final full local rerun uses an exact native Linux source copy. SHA256
  comparisons across **677 code/test/tool files** found zero differences;
  manifest digest `8defee198e1ebc6fa1ce6fdfc7adc597f3f0eb55483d26b6257ff6dbe6ca2740`.
  Its retained log is `u05a-final-full-tests.log` in the persistent Linux
  validation directory. `UNIVERSAL_U05A_VALIDATION.json` records the source
  identity and immutable validation receipts available at documentation capture.
* Host paired runs used three Root agents, active Candidate, concurrent real
  policy fit/model writes, Correct and production storage/thread startup.
  Input and decision hashes match on every pair. Shared event-to-decision p95:
  17.77 -> 17.11 ms; independent: 15.04 -> 15.80 ms. Same-source independent
  A/A varied 16.72 -> 20.93 ms. No causal speedup/no-regression bound is claimed.
  Exact samples, CPU, RSS, fixture flags and hashes are in
  `benchmarks/UNIVERSAL_232_HOST_PARITY.json`.

Validation environment: WSL Ubuntu, Python 3.12.3, NumPy 2.3.3. Native Windows
is not the full-suite acceptance environment because of existing process tests.
Persistent Linux validation tree and venv:
`/home/mpuchara/homemind-universal-232-validation/`.
The first attempt stored logs in `/tmp`; WSL restarted and cleaned that directory.
Its successful test result is retained in the tool transcript and above.
Subsequent logs and native snapshots use the persistent directory.

Useful commands from this checkout:

```powershell
wsl -d Ubuntu -- bash -lc 'cd /mnt/c/Users/mpuch/OneDrive/Dokumenty/HomeMind/HomeMind-HA-universal-232 && /home/mpuchara/homemind-universal-232-validation/venv/bin/python -m unittest discover -s tests -p "test_universal*.py" -v'
```

For faster full-suite runs copy the exact current checkout to the native Linux
`current/` tree first, excluding `.git` and `__pycache__`, then run:

```sh
cd /home/mpuchara/homemind-universal-232-validation/current
../venv/bin/python -m unittest discover -s tests -v
```

## Open gates and exact continuation order

1. Inspect final HEAD and PR checks; do not infer current CI from an older SHA.
2. Keep U05a as offline preparation. Existing observers do not persist historical
   registry/device-mask receipts or completeness watermarks. A durable opt-in
   producer is still needed before operational training. Missing historic
   evidence must reject samples; never fill it with today's HA or registry.
3. If extending data collection, persist exact read-snapshot/coverage revisions,
   historical dependencies/affected-sensor exclusions and decision authority.
   Reuse existing provenance and journals; keep this work isolated and bounded.
4. U05b gets a separate branch/PR based on accepted U05a: freeze U03 initially,
   train the shared scorer with real gradient updates, version checkpoints,
   preserve dataset/supervision revision identity, resume safely and recheck
   current undo state before publication. Rebuild an isolated child on revocation.
5. U05c adds local preference adapters and multipoint Correct/undo integration
   without changing the parent or unrelated devices. Preserve existing UI.
6. U05d uses the existing generation lineage, Candidate/Shadow comparison,
   promotion validation and rollback. No parallel Candidate Manager.
7. Before release/control, measure the physical Pi4 event-to-intent matrix,
   observer-enabled CPU/RSS, intense events/training, HA WebSocket continuity,
   dispatch/state ACK and soak. Host timing, CI and a Docker smoke do not prove
   physical-device acceptance. No installation or Pi4 run occurred here.

The scorer/encoder are still untrained. Both universal observer options remain
false. There is no new physical actuation, automatic model migration, promotion,
installation release or version bump. All real actions retain the existing
DeviceAgent -> ActionIntent -> Executor boundary.

## Acceptance checks on continuation

Resolve PR #356's current check status and read the tail of the exact-source
local log before making an acceptance claim. This document records immutable
source/test receipts and links to the acceptance runs, whose final live states
can change after documentation capture. No later U05 stage should infer
trained-model or physical acceptance from these data-contract checks.

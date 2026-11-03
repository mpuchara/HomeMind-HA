# U05b.3: validated offline neural checkpoint lifecycle (2026-11-03)

## Checkout and immutable references

Worktree: `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`.
Branch: `codex/universal-u05b3-artifact-lifecycle`.
Draft PR: [#359](https://github.com/mpuchara/HomeMind-HA/pull/359), targeting
`codex/universal-u05b2-quality-gate`, **not main**.
Last implementation commit: **b4c5c74dea2a84b912fa7e2bcc1ccb2cafaa9c4f**
(main lifecycle: `2eef68333f842ef668eff51d9b888d1b54607bf5`; isolated benchmark
bootstrap: `a232841d1e0194397ee7140b1477ae6031f1559b`).
Documentation receipts may follow; obtain the exact current HEAD with
`git rev-parse HEAD` and verify its live CI before continuing.

Before branching, U05b.2 PR #358 HEAD was verified as
**6a9beaa30adbf7f848521a68ea323bb14a1962e5**. Its complete CI
[41548469162](https://github.com/mpuchara/HomeMind-HA/actions/runs/41548469162)
passed Python 3.11/3.13, image and required benchmarks; package was skipped.
Production reference stays **0.14.232**, commit
**f7384ba5476f26155ecb138034cc3350fa5bfda2**. No version/release/package change.
The stacked Draft chain #355 -> #356 -> #357 -> #358 -> #359 is not merged.

Read predecessor documents `UNIVERSAL_U05B_HANDOFF.md`,
`UNIVERSAL_U05A_TRAINING_DATA.md`, `UNIVERSAL_U05B_NEURAL_CORE.md` and
`UNIVERSAL_U05B2_QUALITY_GATE.md`. This handoff supersedes their statements
that validated offline publication is unimplemented. Operational collection,
full U05b completion and physical acceptance remain open.

## Implemented contract and trust boundary

`adaptive_ai/src/universal_checkpoint_lifecycle.py` is an explicit offline
service; nothing imports it into Engine, WebSocket, RuntimeComposition,
Correct, Candidate/Shadow, DeviceAgent or the runtime trainer.

`NativeDatasetSource.create(...)` detaches the caller's historical decision
receipts, registry epochs, coverage and as-of boundary into canonical bounded
JSON. It stores **no alternative Correct history**. At every publication or
guarded load, U05a `prepare_dataset` reads the native ManualFeedbackJournal,
Teach-RL effect links, label event ids/tombstones, FeatureJournal and archive
from one consistent SQLite snapshot. Conflicting corrections, inactive labels,
changed event ids/generations/revisions and altered historic observations cannot
silently reuse the previous dataset identity. Undo is evaluated in current
native state even when the receipt's as-of time precedes Undo.
The additive U05a `supervision_as_of_ts` option lets this caller include **all
currently recorded** native facts/tombstones, including later-created generation
copies and delayed corrections, while observations remain pinned to historical
`as_of_ts`. Existing callers keep their previous default. A late duplicate Undo
cannot hide behind the historic cutoff; label-availability split rules still
prevent training on a correction before it was known.

The distinct version-1 `HomeMindValidatedNeuralArtifact` contains the complete
U05b.1 core and recomputed U05b.2 report. Content hashes bind model/core,
encoder, dataset, supervision revision, historical source, policy, holdout,
attempt and report. Artifact id covers the whole contract; an issuer HMAC-SHA256
seal authenticates it within the trusted local Store. The issuer key and full
artifact bytes are durable in that same database. The new envelope alone has
`trained=true`, `qualification=offline_shadow_only`, `control_eligible=false`,
`promotion_eligible=false`. Its nested core retains the older unvalidated flags;
the older core loader does not accept the new envelope as a runtime model.

The trusted boundary is the native DB/issuer key and offline application code.
HMAC is not an external signature or protection against arbitrary Python/DB
writers with that key. Caller JSON `trained=true`, an edited checksum or a
supplied `passed` report cannot publish: the factory reloads the core, reads
native evidence and runs the validator itself. Malformed/corrupt accepted bytes
fail closed and become `corrupt` on guarded recovery.

## Publication, invalidation and recovery

1. Open a dedicated Store connection with SQLite `synchronous=FULL`. Runtime
   cached connection sessions are explicitly refused.
2. Read U05a data in a transaction and record `PRAGMA data_version` on this exact
   connection. Discard the snapshot if another connection committed during it.
3. Reserve the model/policy/source/holdout attempt in a committed transaction
   **before quality evaluation**. Recompute the heavy quality gate without a
   write transaction or Store-wide lock.
4. Acquire `BEGIN IMMEDIATE` and compare the same connection's `data_version`.
   Every intervening external commit, including Undo, native conflict updates
   or explicit recovery, prevents publication. The counter's absolute value is
   never persisted or compared across connections/restarts. The service's own
   reservation commit is safe: it changes only lifecycle tables.
5. Commit the complete sealed artifact BLOB, qualified registry row and
   published-attempt status together. Exception, disk/write fault or process
   exit before commit leaves no accepted artifact. Nothing replaces a model file
   or writes an active-model pointer.

`load(id, source)` requires a matching complete registry entry, valid seal and
current dataset/revision. The returned `QualifiedOfflineModel.rank_context`
repeats this guard before and after raw diagnostic inference, discarding output
if evidence changes. Undo makes the next guarded use fail; stale qualification
is permanently stored as `revoked`. There is no worker proactively rewriting
rows at Undo time. A DB status field alone is not permission: qualification is
always a current-evidence predicate. `revoke(id, reason=...)` explicitly retires
an experiment without deleting bytes or holdout claims.

`recover()` permanently changes pending `validating` attempts to `interrupted`.
It never accepts them. It is safe even if another validator is alive: that
external commit defeats the validator's final data-version check. Conservative
recovery burns the original holdout; use fresh future evidence. No optimizer
cursor/RNG-state continuation is provided: restart rebuilds deterministically
from the clean seed. SQLite atomicity/FULL plus injected abrupt exits are tested;
this is not a physical power-cut/storage-controller certification.

The caller must preserve `source.raw` alongside the trusted native database
backup; it is receipt metadata, not reconstructed from current HA registries.
Missing/wrong receipts or damaged native provenance fail closed. Copying only a
checkpoint JSON/report into another DB does not recreate acceptance. Backing up
the trusted DB must use SQLite's consistent backup procedure, including its
issuer and exposure ledger; do not delete that ledger to reset a holdout.

## Holdout protection and isolated rebuild

Durable claims cover supervision event ids, episodes, physical target/property
decision clocks and fixed protocol 60-second buckets. Label changes, source
renaming, alternate model weights or policy ids cannot turn the same exposed
cases into independent evidence. Failed/cancelled/interrupted attempts retain
claims. Revalidating the exact published core/policy/source recomputes the same
report and increments a reproduction count; it reports
`independent_evidence=false`, creates no new artifact and cannot upgrade a
revoked model. Policy/model changes need a disjoint future holdout. This is
conservative across agents sharing a time bucket.

`rebuild(source, parent_seed=..., encoder_seed=..., predecessor_id=..., **split,
config=TrainingConfig(...))` builds fresh U03 scorer/encoder instances and calls
the existing detached Adam trainer. No parent tensor is accepted, edited or
mathematically unlearned. Optional `predecessor_id` is authenticated and linked
in the new checksum receipt; its weights are never read into the child. Current
native data is reassembled before training, and publication rechecks it later.

Lifecycle builds use `evaluate_holdout=False`: before/after holdout receipts
contain counts and `withheld`, not losses/accuracy. The old trainer's default
diagnostic behavior remains compatible, but its disclosed checkpoints are
rejected by this publication API. Train-only early stopping and normalization
stay unchanged. A rebuild after Undo may optimize on current data, but cannot
qualify on its predecessor's consumed holdout; collect a new future partition.

The ledger cannot prove statistical independence or detect external/manual
peeking performed outside this service. Historical production registry/legal
mask/completeness collection still does not exist. Do not fabricate receipts.
The existing 2048-row U05a cap and per-device support rules remain unchanged.

## Budget and runtime boundary

Existing `TRAINING_BUDGET` checkpoints cover native replay, training and quality
evaluation; lifecycle admission and claim loops also yield. One lifecycle
operation per process is admitted; the existing trainer retains its own fit
admission. Limits: source 16 MiB, core 4 MiB, envelope 8 MiB, 256 attempts,
65536 holdout claims, 64 MiB retained artifact payload. Capacity exhaustion fails
closed and does not erase exposure history. Native replay keeps existing sample,
token, action and feature-byte caps. No unbounded retention worker is installed.

Final write transactions serialize only bounded lifecycle operations; expensive
dataset/feature validation is outside them. Unrelated external commits may
conservatively abort acceptance. Retry may require a fresh holdout after a
reservation; this favors safety over liveness. Guarded inference intentionally
does bounded DB/model verification and is **offline only**, not a low-latency
WebSocket API or a newly enabled Shadow consumer.

No HA service calls, ActionIntent/Executor, Control permission, active model
changes, automatic promotion, migration, default workers or install package.
Existing production runtime mechanisms were not replaced.

## Validation receipts

Implementation-head CI:
[41553029546](https://github.com/mpuchara/HomeMind-HA/actions/runs/41553029546).
**Completed successfully** for implementation commit `b4c5c74d`: Python 3.11,
Python 3.13, image build/smoke and every required benchmark. Package was skipped.
The following documentation-only commit does not change executable code; inspect
its separate automatic CI live rather than attributing this run to another SHA.

| Final evidence | Result |
| --- | --- |
| Local Linux Python 3.12 / NumPy 2.3.3 | 2988 tests, 391.415 s, OK, exit 0 |
| CI Python 3.11 | 2988 tests, 202.865 s, OK |
| CI Python 3.13 | 2988 tests, 235.226 s, OK |
| CI image and all required benchmarks | success |
| Installation package | skipped |

Focused predecessor/scorer tests: **35 passed**. Final focused lifecycle suite:
**23 passed in 31.418 s**, including real subprocess `os._exit` during validation
and after artifact insertion before acceptance commit, rebuilding after native
Undo, predecessor isolation, later-created lineage tombstones and late Correct.
An earlier complete Linux suite passed **2986 tests in 392.193 s** before the
last late-supervision fix; that earlier run is not the final code receipt.

Windows Python 3.11 lifecycle benchmark passed; checked-in
`UNIVERSAL_U05B3_BENCHMARK.json`: publication/validation .567 s, restore .071 s,
guarded projection .360 s, reproduction .323 s, complete artifact 178599 bytes,
172 holdout claims, feature matrices 125952 bytes, temporary database 778240
bytes. These are a single synthetic host run, not production p95/HA/Pi/RAM
acceptance. CI adds `benchmark_universal_lifecycle.py` and retains every existing
Python 3.11/3.13, image and runtime benchmark job. Package stays skipped off main.
Final-code Linux synthetic lifecycle smoke also passed; see
`UNIVERSAL_U05B3_BENCHMARK_LINUX.json`: publication .342 s, restore .351 s,
guarded projection .334 s, reproduction .352 s and Undo invalidation .375 s.
Standalone benchmark import is isolated into a temporary data directory before
`storage` initializes; it does not open default `/data` or caller production data.

Full U05b.2 cardinality benchmark on this stage passed its expected assertions:
5/1 sensors/devices: `passed`; 50/10, 200/30 and 1000/200:
`insufficient_evidence`. Validation times .26/.60/3.01/16.65 s. Larger cohorts
were not accepted by reducing per-device support requirements.

Full Linux command (native ext4 copy, NumPy 2.3.3):

```sh
cd /home/mpuchara/homemind-universal-u05b3/current
/home/mpuchara/homemind-universal-232-validation/venv/bin/python -m unittest discover -s tests -v
```

Persistent local logs: `/home/mpuchara/homemind-universal-u05b3/logs/`:
`full-tests.log` (earlier code), `full-tests-final.log` (final code),
`quality-benchmark.json`, `lifecycle-benchmark-final.json`.
Lifecycle source SHA256 in the native final-test copy and Windows worktree:
`d0ec27c7af9bd478c54c3c82b58e1c015d00322360475c4bb246e072fa050725`.

## Continuation

1. Verify #359 current HEAD/CI and exact dependency #358 before edits. Refresh
   the native Linux copy before tests; do not reuse results from different code.
2. Use existing native U05a schemas in an explicitly offline Store, preserve
   historical receipt bytes and the DB/issuer/exposure ledger, then instantiate
   `CheckpointLifecycle`. Use `rebuild`, `publish`, `load`, `revoke`, `recover`.
   Never enable the disposable `tools/universal_training_fixture.py` in runtime.
3. Continue **U05b.4 performance/acceptance** and production historical data
   collection. Repeated host/load/resource measurements and actual Pi4/HA/UI/
   dispatch/state-ACK/soak evidence remain required before operational changes.
4. Keep all Control, active-model and promotion boundaries closed. Do not merge
   the Draft stack, change 0.14.232 or publish an installation package.

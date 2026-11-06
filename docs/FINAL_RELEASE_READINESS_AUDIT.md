# Final release readiness audit — 2026-11-06

Overall: **BLOCKER**. Source version remains **0.14.232**. This is an
integration/development checkpoint, not an install recommendation.
Starting HEAD: `07ed423daa760bce2c1903448719cfe711b4f06b`, active production
runtime `ab7116f585a2016c3a4a2cf6d513a7a62c3b40fb`, performance baseline
#368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.

## GitHub and integration

The current GitHub inventory is saved in
[FINAL_RELEASE_GITHUB.json](benchmarks/FINAL_RELEASE_GITHUB.json). All 68 open
PRs (#302–369) were inventoried; #349–369 were individually read for draft,
mergeability and exact refs. They are open Drafts and GitHub reports them
mergeable. This means a textual merge is available, not release approval.
Current main is `91c7343324d38a1922bfed6486b4ef6dd7a3ba06`, version 0.14.184.
Latest published prerelease is 0.14.231-rc.1 (`a5b07f0c`); stable is 0.14.184.
Exact starting HEAD CI completed successfully:
[41971796849](https://github.com/mpuchara/HomeMind-HA/actions/runs/41971796849),
[41971791244](https://github.com/mpuchara/HomeMind-HA/actions/runs/41971791244).
These runs do not validate subsequent commits or physical HA.

Check-runs were also fetched for **every saved open PR HEAD** in
[FINAL_RELEASE_PR_CHECKS.json](benchmarks/FINAL_RELEASE_PR_CHECKS.json).
No check-runs were returned for #346/#347; this is missing evidence, not PASS.
#316's HEAD has a failed 3.13 run and a cancelled 3.11 run alongside successful
runs for the same SHA. Preserve the distinction between individual workflow
attempts; do not describe the entire draft stack as uniformly green. All other
saved heads returned successful Python 3.11/3.13 and image checks. Package jobs
were skipped for these branch checks.

`python tools/audit_release_dependencies.py --output NEW_FILE.json` verified
every base is an ancestor of its own PR head. All open heads except #347 are
ancestors of #369. The machine-readable result is
[FINAL_RELEASE_DEPENDENCIES.json](benchmarks/FINAL_RELEASE_DEPENDENCIES.json).

The two branches after #346 matter:

- #347 (0.14.231-rc.1) is a sibling of #348, not its ancestor. The #369 runtime
  retains the older ingress copy and complete-map bookkeeping. Inspection of
  the two runtime file diffs confirms those optimizations are absent. Integrating
  them requires an isolated benchmark and semantic review; do not overwrite the
  current engine or cherry-pick its version/packaging changes wholesale.
- #349–354 extend main with U01–U04c. #355 already merges that history onto
  pinned #348 (0.14.232); `docs/UNIVERSAL_232_COMPATIBILITY.md` records conflict
  resolution. Integrating #355 after #348 carries both histories; merging the six
  old branches separately is unnecessary. #356–369 then form the linear stack.
- #302–346 supply the earlier 0.14.185–230 runtime. Their current heads are
  already ancestors. Ancestry alone cannot prove every later override equivalent;
  composition, migrations and behavior remain required acceptance gates.

## Owning runtime and data paths

`adaptive_ai/src/run.sh` starts `trial_queue_main.py`, importing
`preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`.
`runtime_composition.bind_final_composition()` binds final extensions before
startup/train, quiet-start and UI lifeline guards. Final composition explicitly
installs generation-aware Correct history after the durable workflow queue and
before explicit Workflow read routes. Candidate, neural Shadow, provenance,
automatic Correct and Universal workers have dependency-sensitive installers.
Tests of individual classes do not certify this entire composition under load.

Supervisor supplies persistent `/data`; `ADAPTIVE_AI_DATA` can override it for
disposable tests. `settings.py` selects `/data/adaptive_ai.db` and `/data/options.json`.
SQLite contains models, agents, lineage, feedback, training state and auxiliary
history. Keep the **entire add-on data directory**, WAL-aware DB state and options
together in a backup. The add-on declares `backup: hot`; a raw copy of only the
main DB during writes is not an established consistent backup procedure.

`Store.initialize` performs additive schema/setup stages and `migrate_models`.
The storage legacy guard accepts policy >=10/schema >=11 and defers exact
compatibility to the observation contract. Older/unparseable models are backed
up and their agents paused/marked `needs_retrain`; therefore no universal
no-Rebuild guarantee follows from reading the migration code. Validate actual
prior-version fixtures and all runtime installers before permitting upgrade.
Rollback by application downgrade alone is unproven.

The supported Core floor remains **2026.1.2**, audited at official Core SHA
`ae17b620f69b7a132d32d2de1e30097f9a251616` in
`docs/UNIVERSAL_U05B4C1_HA_NATIVE_AUDIT.md`. The audit is source-level;
current HA permissions, Recorder and physical-device behavior are **NOT RUN**.
Universal observer, action observer and historical collector remain default-off
in `config.yaml`; offline scorer/quality/checkpoint work is not a Live deployment.

## Readiness matrix

| Obszar | Stan | Ryzyko | Dowód | Działanie | Blokuje wydanie |
| --- | --- | --- | --- | --- | --- |
| Git dependency inventory | PASS (ancestry only) | Sibling #347 omitted; drafts unapproved | Saved refs + local ancestry | Review sibling separately; approve integration stack | Yes, integration approval pending |
| Entrypoint/wrappers | PASS (source audit) | Late installer override / lifecycle interaction | run.sh, trial_queue_main, runtime_composition | Exercise exact shipped composition with WS and HTTP | Yes |
| HA Core compatibility | PASS (pinned source scope) | Real Recorder/permissions/reconnect unknown | HA-native audit, Core 2026.1.2 | Disposable controlled transport and separate physical acceptance | Yes for composition; physical status separate |
| Gate D performance | BLOCKER | Normal CPU and p99 breaches; host variance | #369 report/protocol/raw data | Profile and compare fixed workload against exact #368 | Yes |
| Snapshot ownership | PASS (five new regressions) | Scoped copy could omit remapped/sibling inputs | test_final_release_snapshot_scope.py, scope profile | Preserve full snapshot; evaluate only measured redundancy | Yes until full performance/safety validation |
| Candidate recovery | NOT RUN (new acceptance matrix) | Evictions, stale generations, stalled training | Existing bounded queues and #369 load counters | Overload, drain, fresh wave, interruption and generation tests | Yes |
| Correct/Undo/history | NOT RUN (new composition acceptance) | Historical Current/Desired or correction evidence lost | Explicit installers; existing tests | Actual HTTP routes, concurrent updates and restart | Yes |
| SQLite/model migration | NOT RUN (upgrade acceptance) | Legacy guard can pause models; later schema changes | storage.py, observation_contract, installers | Prior-version data copy with checksums and restart | Yes |
| Storage/retention | BLOCKER | Prior intermittent rollback receipt miss unresolved | #368/#369 stability reports | Preserve failure; test order and retention boundaries | Yes |
| Four-hour soak | NOT RUN | Existing 12.677s run is not a soak | benchmark_qualified_soak.py, prior report | >=14400s with full required activities and resource trends | Yes |
| Final CI | NOT RUN (future final SHA) | Existing green CI is not final validation | Starting HEAD runs linked above | Full 3.11/3.13, benchmarks, image on final SHA | Yes |
| Packaging/publishing | BLOCKER | main green CI automatically publishes | publish-install-release.yml, package_install_release.py | Keep main unchanged until all gates; verify exact committed ZIPs | Yes |
| Pi / real HA | NOT RUN | Synthetic host cannot certify device behavior | No device operation in this task | Separate authorized acceptance; label honestly | Separate physical acceptance |

## Concrete first experiment

New snapshot tests cover same-device siblings, explicit inclusion/exclusion,
deep ownership, concurrent relevant-state change, registry-revision change and
mapping changes after the native SQLite snapshot. First attempt failed because
the fixture modified `resolved_registry()`'s returned copy; corrected to mutate
the fixture registry owner. The corrected five tests passed on WSL ext4.

The opt-in profiler records descriptor scopes and actual state capability reads.
At 1000 sensors/200 roots, fanout 16, 24 ticks/400ms, the fresh profile preserved
384 original intents with zero HA calls and no preparation errors. It observed
31 preparation turns (including finalization) and **93 descriptor evaluations**.
The full state and registry snapshots each copied 37,200 entries across turns.
The next isolated candidate can reuse the same target descriptor within one
detached preparation turn. It must not add a live cache, skip full snapshots,
change clocks/revisions/mappings, or change acceptance thresholds.

# Performance optimization continuation

CURRENT VERSION: 0.14.231 (test candidate)
CURRENT BRANCH: codex/inference-capture-0.14.231
BASE COMMIT: d460e0e10f18bcfebcd4565dcfdf54bf8782157b, 0.14.230 HEAD / PR #346
BASE BRANCH: codex/performance-capture-20261027
PR: https://github.com/mpuchara/HomeMind-HA/pull/347
GREEN EXECUTABLE/TEST/TOOL SNAPSHOT: fbf9f16e4a3306f56118261c6572cfb7ea92aea4
CHECKOUT: C:/Users/mpuch/Documents/Codex/2026-10-28/pracujemy-na-repozytorium-mpuchara-homemind-ha/work/HomeMind-HA

Never restart from main or merge automatically. A documentation/receipt commit
follows the green source. Resolve final HEAD with git rev-parse HEAD; both install
packages record it in SOURCE_COMMIT.txt. Verify committed-byte hashes against
docs/benchmarks/PERFORMANCE_0_14_231_SOURCE_HASHES.json. Windows checkout CRLF is
not the CI/git-archive byte tree.

## Completed code and measurements

- Engine.run checks only state presence and lets process take the sole existing
  authoritative atomic states/revisions/context/receipt snapshot. Event, startup
  and timer workers keep the same shared detached snapshot and Executor guards.
- Existing bounded DetachedMappingSnapshots collects eligibility in its record
  loop and conservatively accounts fixed-size identity/part containers without
  redundant full-map scans. Mutation/type detection, limits and eviction remain.
- No learning, features, reward, decisions, Correct, Candidate, Shadow, FIFO,
  durable Control, promotion/rollback, gaps or storage-budget changes.
- Scheduler-only five paired series, builtin dict, 5000 passes: median paired
  wall ratio after/before 0.912. Separate tests confirm two copies become one.
- Three paired capture series, 780 entities, 80 warm frames: approximately 4.2%
  lower time; every complete causal-frame hash matches. Policy serialization
  under lock is retained because champion identity does not prove immutable weights.
- Full shipped Root matrix 1/4/5/10/20 agents, shared/independent, 780 entities:
  pilot three pairs x120 events +12 warmup; confirmation three pairs x300 events
  +24 warmup, complete finite benchmark window instead of only the production ring.
  Additional one-agent three pairs x1000 events per topology. All decision hashes
  match and every run asserts zero HA service calls.
- Preserve the mixed end-to-end results, including slower cases. No uniform
  full-runtime or target-device speedup claim. Same-source four-agent control
  (five pairs) p95 ratios range 0.916..0.977.
  Read the Polish report and complete SCALING/RAW evidence before further tuning.

## Validation

Pełne końcowe CI: Python 3.11 - 2822 testy OK / 158,431 s; Python 3.13 - 2822 testy OK / 266,084 s. Wszystkie dodatkowe kroki benchmarków, składni, entrypoint i job image zakończyły się sukcesem. Końcowy push candidate_packages również zakończył się sukcesem, budując oba layouty istniejącym skryptem. Lokalnie pięć nowych regresji: OK. Nie powtarzano lokalnie pełnych suit po zielonych pełnych testach CI.

CI: https://github.com/mpuchara/HomeMind-HA/actions/runs/40665140650
Push/install-package CI: https://github.com/mpuchara/HomeMind-HA/actions/runs/40665129710

Initial CI found two source-text assertions expecting process(state_map,...).
They now expect process(None,...) without weakening ordering or timer guards.
Added worker-profiler hooks failed on Python 3.12 and were removed. Use the existing
bounded RuntimeDebugTrace for actual worker CPU timing; final CI covers debug mode.
Invalid profiling attempts are excluded from acceptance. Do not repeat green full suites without a new change/failure.

## Reports and packages

Report: docs/PERFORMANCE_OPTIMIZATION_0.14.231_PL.md
Evidence: docs/benchmarks/PERFORMANCE_0_14_231_{SCALING,QA,PROFILE,SOURCE_HASHES}.json
All raw runtime windows: docs/benchmarks/PERFORMANCE_0_14_231_RAW.json.gz

After committing intended content: python tools/package_install_release.py
Builds exact committed bytes, SOURCE_COMMIT, per-file SHA manifest, CRC/version/
layout/LF checks. Outputs dist/HomeMind-HA-0.14.231-{repository,addon}.zip and
checksums. Final public test-candidate assets:
https://github.com/mpuchara/HomeMind-HA/releases/download/v0.14.231-rc.1/HomeMind-HA-0.14.231-repository.zip
https://github.com/mpuchara/HomeMind-HA/releases/download/v0.14.231-rc.1/HomeMind-HA-0.14.231-addon.zip

Back up data/previous version before manual installation; replace sources,
rebuild the image and restart, preserving data. No model Rebuild is required.
No automatic merge or physical installation was performed.

## Continuation after this host release stage

Installed Pi latency/CPU/RAM, HA dispatch and observed state ACK, active Candidate/
concurrent training/Correct scaling and 24h soak are separate target acceptance.
Use the preserved 0.14.230 report's target procedure with identical input/config/
storage, tracing off, bounded diagnostics, restart/rollback and explicit gaps.
Never weaken Control durability, Correct/model-generation safeguards, promotion/
rollback, FIFO or RAM/disk/WAL bounds for timing results. This task's work/ helper
files and /var/tmp/hm231/results retain additional host orchestration evidence.

# U05b.4c.1 final handoff

Draft [#366](https://github.com/mpuchara/HomeMind-HA/pull/366) stays on
`codex/universal-u05b4c1-runtime-recovery`, stacked on Draft #365. Tested
implementation `8036a2bd64dba32490d784df2029c0818ad3edbf`. The addon version is
0.14.232 and the audited HA Core compatibility floor is 2026.1.2.

The [recovery report](UNIVERSAL_U05B4C1_RUNTIME_RECOVERY.md) records host
passes for A, B, C and E, with D **UNRESOLVED** and F **NOT TESTED**.
The reporting commit is separate from the measured implementation. The original
audit [handoff](UNIVERSAL_U05B4C1_HANDOFF.md) remains the record of substage 1a.

## Validation provenance

- Exact implementation [CI 41756698124](https://github.com/mpuchara/HomeMind-HA/actions/runs/41756698124)
  passed Python 3.11 and 3.13 (3,131 tests each), the required benchmark smokes
  in their configured jobs, and the image build and image smoke. Packaging was
  skipped. Local WSL ran 3,131 tests in 438.200 s with exit 0. The collector
  fault-focused run passed 38 tests in 13.526 s.
- The fault tests exercise a reusable connection open failure and recovery,
  retained records on SQLite busy, stale epoch and watermark rejection,
  original receipt content, transactional rollback, bounded batches, V3 prune
  invalidation and clean shutdown/restart. The archive also includes the
  pre-fix failure and a failed intermediate test setup.
- The primary probed matrix uses `42f14bb1c5050428d93a0b4ce74706ed6e6ad125`
  for current and current-off. Final unprobed and the two final 1000/200 probed
  confirmations use `8036a2bd`. The subsequent production code delta is the
  collector worker entry/recovery path. The earlier matrix is retained under
  its own source identity. The overload matrix and E2E fixture serve different
  acceptance purposes.
- Raw and adverse results, matrix runs, source manifests, runner scripts,
  local regression logs and CI evidence are in
  [u05b4c1-recovery](benchmarks/u05b4c1-recovery). The
  [JSON entry point](benchmarks/UNIVERSAL_U05B4C1_RECOVERY_SUMMARY.json)
  links all series and gives the exact results per 1000/200 unprobed pair.
  The original WSL result tree remains under
  `/home/mpuchara/homemind-u05b4c1-recovery/results`.

## Resume Gate D

For another diagnostic pass, use a new output directory and run cases serially
in WSL, separate from full-suite tests. Use the published drivers for the 40/8
workload. Keep the baseline 120 s series diagnostic, retain the original
30 s deadline for acceptance, and record failed observations.

Start with a matched-work sensitivity check at 1000/200, with a fixed number
and cadence of background updates/model writes per phase and the same
Candidate, Correct/Undo and ingress settings on both sources. Compare a
separate serial probe series for executor admission, runnable/in-flight
workers and SQLite lock/writer intervals. Preserve all four prior pairs.
Use `tools/benchmark_runtime_scaling.py --help` and
`tools/benchmark_runtime_recovery.py --help` to reproduce the existing
workload, and `tools/benchmark_universal_e2e.py --output <new-result.json>`
for the existing four-scale V2/V3 fixture.

For targeted correctness after a runtime change, run
`python -m unittest discover -s tests -p 'test_universal_history_collector.py' -v`,
`python -m unittest discover -s tests -p 'test_universal_e2e_validation.py' -v`
and `python -m unittest discover -s tests -p 'test_universal_retention*.py' -v`,
then the full `python -m unittest discover -s tests -v` and exact-head CI.
The Raspberry Pi and real HA gate requires separate hardware measurements.

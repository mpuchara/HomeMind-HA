# U05b.4b.4 continuation handoff

Branch: `codex/universal-u05b4b4-e2e-neural-validation`.
Base: exact PR #363 `e797439989dba56ca2ccf3d37cf1b3521c501b1c`.
Implementation HEAD: `e6d13526f0bf30ab82d35483d24502c9bd5443eb`.
Benchmark-only observation/warmup follow-up:
`1a3a79abec6b8ec18bb1452ea6830d3ff8be87d3` (two tools only, no runtime module).
For the current documentation/results HEAD use `git rev-parse HEAD`; a document
cannot embed the hash of the commit containing itself. Subsequent commits must
remain documentation/measurements only unless separately revalidated.

Read [contract, implementation and results](UNIVERSAL_U05B4B4_E2E_NEURAL_VALIDATION.md)
for changed modules, all implemented functions and the qualification boundary.

## Recovered interrupted session

No reset, cleanup or rearchitecture was performed. The branch already existed
at the #363 HEAD with four modified tracked files and three untracked files.
Those changes were preserved and committed in the implementation above.
There was no running Python test/benchmark process in Windows or WSL at resume.

`.devtools/u05b4b4-fullsuite-linux.log` ends with **3100 tests / 459.068 s / OK**.
The original process exit code and an exact source manifest were not recorded;
the historical result is not treated as exact-commit validation.
`.devtools/u05b4b4-e2e-final.log` ends with **24 tests / 64.720 s / OK**.
Earlier development logs are also preserved: `u05b4b4-positive.log` contains
three test-fixture `TrainingDataset object is not callable` errors, and
`u05b4b4-e2e-full.log` contains one manual-hold fixture assertion failure.
The recovered final tests already correct those fixture mistakes; they are
not unresolved failures hidden behind the final successful footer.
The previous two-scale smoke completed but preceded the last benchmark edit;
its isolation cannot be established. All original logs remain intact.
Recovered tracked diff and untracked inputs are backed up under
`.devtools/u05b4b4-resume-20261103/`.

Fresh WSL snapshots/results:
`/home/mpuchara/homemind-u05b4b4-resume-20261103/{current,base-363,results}`.
The baseline worktree was clean at the exact requested #363 SHA before copying.
The results directory retains manifests, stdout/stderr, exit codes and raw
runtime logs. Benchmarks run sequentially, followed by the full regression.

## Current outcome

Four-scale lifecycle measurements completed with exit 0. Eligible samples:
128, 128, 128, 200; rejected: 0 at each scale. Real updates: 264, 264, 264, 408.
5/1 passes the unchanged gate, publishes and performs guarded load/inference;
50/10, 200/30 and 1000/200 correctly remain insufficient_evidence. These are
repeatable synthetic regression trials, not newly independent household data.
All fixture intents have native provenance and HA calls are zero. Published
test artifacts are disposable, offline-only and not installed.

[Draft PR #364](https://github.com/mpuchara/HomeMind-HA/pull/364) targets #363's
branch. [CI 41710898166](https://github.com/mpuchara/HomeMind-HA/actions/runs/41710898166)
for documentation/results HEAD `bda4aa366af51123d9cb761bdf2e5ed45fb22e6f`
completed success: Python 3.11 **3100 tests / 324.892 s**, Python 3.13
**3100 tests / 339.169 s**, image and all required benchmark steps passed;
package skipped. Its executable code is identical to implementation e6d13526.
The CI job/step record is `benchmarks/u05b4b4-ci-41710898166.json`.

Initial controlled runtime sweep completed the first three scales with input,
decision and event-bound ActionIntent parity and zero HA calls. At 1000/200,
baseline A and current-off completed, but current A failed the existing
30-second event-step wait (169 of 200 agents seen, Engine.error=None). Exit 1
and the original logs/partial metrics are retained in
`benchmarks/u05b4b4-runtime-incomplete-aa.json` and
`benchmarks/u05b4b4-runtime-adverse-summary.json`. Full raw probe samples are
preserved losslessly in `benchmarks/u05b4b4-runtime-adverse.json.gz`, with
uncompressed SHA-256/size in the summary; the original uncompressed file also
remains local and is ignored by git. This was not overlapped
with full regression and must not be labelled a successful sweep. The fresh
full WSL suite runs next, followed by a separate full 1000/200 A/A control
sweep. These remaining results are pending final recording; U05b.4b.4 is not
yet declared complete.

The first fresh full WSL run executed 3100 tests / 418.766 s / exit 1: one
FileNotFoundError because the copied snapshot omitted root test input
`BENCHMARK_PRODUCT_F24.md`; the other 3099 passed. The input is restored without
changing code, and `fullsuite-final.log` is the separate full rerun. Do not
overwrite or call the failed run successful.

The separate original-deadline 1000/200 control also failed, on baseline A
(176/200). Both 30-second-deadline failures remain adverse observations.
Harness follow-up 1a3a79ab adds a bounded, explicit 30..120-second observation
parameter, default unchanged at 30, and reports warmup quantiles. It changes
no runtime deadline or quality/decision gate. Its own CI is
[41714102847](https://github.com/mpuchara/HomeMind-HA/actions/runs/41714102847).
After the full rerun, a fresh `expanded` snapshot runs a five-way 5/1 smoke
(12 events/4 warmup), then the full 1000/200 group (40 events/8 warmup), both
with explicit `--event-timeout 120`. Results/logs use distinct
`runtime-expanded-smoke*` and `runtime-1000-expanded*` names. Long observation
is diagnostic, not a 30-second response acceptance. Neither 30-second failure
is replaced by a later success. No test is run concurrently with these
performance measurements.

## Continuation

1. Inspect existing WSL processes and results before restarting any workload.
2. Preserve all measurement files. Finish four-scale baseline/current A/B and
   collector-disabled runtime reporting, including adverse drops/latencies.
3. Record the fresh full suite's exact source and exit code, then required
   Python 3.11/3.13, image and benchmark CI for the implementation commit.
4. Update this handoff and the contract with those results; maintain one Draft
   PR targeting `codex/universal-u05b4b3-granular-retention`.
5. Never merge, install, activate neural Control, change the active model,
   relax gate criteria or reuse an exposed holdout as independent evidence.

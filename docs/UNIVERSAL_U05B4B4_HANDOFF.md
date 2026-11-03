# U05b.4b.4 continuation handoff

Branch: `codex/universal-u05b4b4-e2e-neural-validation`.
Base: exact PR #363 `e797439989dba56ca2ccf3d37cf1b3521c501b1c`.
Implementation HEAD: `e6d13526f0bf30ab82d35483d24502c9bd5443eb`.
Benchmark-only observation/warmup follow-up:
`1a3a79abec6b8ec18bb1452ea6830d3ff8be87d3` (two tools only, no runtime module).
Reporting snapshot HEAD before this final results update:
`e8acaf99301be22605cc28e71813923d0f97035f`, with successful
[CI 41714791110](https://github.com/mpuchara/HomeMind-HA/actions/runs/41714791110).
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
`/home/mpuchara/homemind-u05b4b4-resume-20261103/{current,base-363,expanded,results}`.
The baseline worktree was clean at the exact requested #363 SHA before copying.
The results directory retains manifests, stdout/stderr, exit codes and raw
runtime logs. Every local full suite and benchmark was sequential: initial
lifecycle/sweep, first full run, original-deadline control, corrected full
suite, expanded-deadline smoke and expanded largest-scale sweep. Nothing
remains running from these jobs at handoff.

## Current outcome

The requested host integration, tests and four-scale measurement deliverables
are complete. This is synthetic offline engineering validation, not household
acceptance, a qualified 200-device model or a runtime performance certificate.
The two original-deadline failures, large warmup tails, optional write losses
and insufficient larger cohorts remain explicit findings.

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
with full regression and must not be labelled a successful sweep.

The first fresh full WSL run executed 3100 tests / 418.766 s / exit 1: one
FileNotFoundError because the copied snapshot omitted root test input
`BENCHMARK_PRODUCT_F24.md`; the other 3099 passed. The input is restored without
changing code. The separate `fullsuite-final.log` completed **3100 tests /
427.301 s / OK / exit 0**, on exact implementation e6d13526, with 720
source/test/CI Git blob identities checked and a separately hashed root test
input. Do not overwrite or call the failed run successful.

The separate original-deadline 1000/200 control also failed, on baseline A
(176/200). Both 30-second-deadline failures remain adverse observations.
Harness follow-up 1a3a79ab adds a bounded, explicit 30..120-second observation
parameter, default unchanged at 30, and reports warmup quantiles. It changes
no runtime deadline or quality/decision gate. Its own CI is
[41714102847](https://github.com/mpuchara/HomeMind-HA/actions/runs/41714102847),
completed **success**: Python 3.11 **3100 / 313.223 s**, Python 3.13
**3100 / 339.737 s**, image and every required benchmark passed, package skipped.
The later e8acaf99 reporting snapshot also passed the complete workflow above.
Their job/step records are retained in `benchmarks/u05b4b4-ci-*.json`.

After the full rerun, a fresh `expanded` snapshot ran a five-way 5/1 smoke
(12 events/4 warmup), then the full 1000/200 group (40 events/8 warmup), both
with explicit `--event-timeout 120`. Results/logs use distinct
`runtime-expanded-smoke*` and `runtime-1000-expanded*` names. Both completed
with **exit 0 / parity=true**. All 720 expanded source/test/CI files match exact
benchmark commit 1a3a79ab. Long observation
is diagnostic, not a 30-second response acceptance. Neither 30-second failure
is replaced by a later success. No test is run concurrently with these
performance measurements.

## Final runtime and qualification limits

[Combined report](benchmarks/UNIVERSAL_U05B4B4_SUMMARY.json) joins the three
completed smaller groups with the separate expanded largest group, without
overwriting any original result. Every group has five variants: baseline A,
current A, baseline B, current B and current collector-disabled. Input hashes,
decision hashes and event-bound ActionIntent counts match within every group;
all **20 completed observations have zero HA service calls**. Intent counts
per variant are **40 / 400 / 1200 / 8000** across the four scales.

| Sensors/devices | Baseline p95 A/B ms | Current p95 A/B ms | Current off p95 ms | Drops in each enabled run |
|---|---:|---:|---:|---:|
| 5/1 | 8.19 / 6.52 | 6.10 / 6.45 | 6.22 | 0 |
| 50/10 | 42.31 / 42.54 | 48.17 / 43.02 | 49.32 | 0 |
| 200/30 | 148.00 / 148.65 | 247.35 / 160.46 | 160.63 | 1065 |
| 1000/200 | 2607.83 / 2056.13 | 2097.53 / 2179.43 | 2235.82 | 9335 |

Largest warmup maxima are **30.52–33.56 seconds**, including collector-off.
These tails are retained separately from measured event latency. The larger
wait records slow completions rather than improving them. Exact cause of the
two earlier 30-second failures remains unresolved. Current 200/30 A is also
materially slower than its B and both baselines. This evidence supports no
absence-of-regression or default-deadline/SLO acceptance claim. Queue peaks,
write p95/max, CPU/RSS, instrumented SQL operations, model prediction latency,
warmup p50/p95/p99/max and raw-log hashes are preserved in the combined report;
the full detailed table and methodological limits are in the contract.

The 2048 source cap cannot provide the required 72 future independent units
for each of 200 device/property slices. No device is silently dropped, no
quality threshold is lowered, and no larger artifact is fabricated. The
single-device fixture artifact passes the actual unchanged gate and guarded
load/inference, then is deleted with its disposable Store. Repeated synthetic
holdouts are not new independent statistical evidence. No deployable household
model is supplied. Pi4/HA/Recorder/physical acceptance remains unperformed.

Original V2 receipts, native V3 coverage, U05a dataset, real shared scorer
updates, frozen encoder/parent isolation, chronological future holdout,
unchanged gate, atomic publication, guarded inference, Undo/retention and
runtime intent/call parity are covered by the tests and observations above.
Required Python 3.11/3.13 and image/benchmark CI are green for all executable
changes; later commits contain only reporting artifacts. One Draft PR and both
requested documents exist. No merge, package/release, live installation,
Candidate Manager update, automatic training/promotion or neural Control was
performed.

## Continuation

1. Inspect branch/head, PR #364 and existing results before restarting work.
   Implementation ends at e6d13526; only the two benchmark tools change in
   1a3a79ab. Later commits are documentation/measurement-only. Check their
   automatic CI separately rather than attributing an old result to a new SHA.
2. Preserve raw logs, original failures and source manifests. Full suites are
   complete; rerun only for changed code or a new unresolved validation issue.
3. If performance follow-up is requested, start with the recorded 30+ second
   warmup and 200/30 A outlier on both exact sources. Do not alter production
   scheduling merely to make this fixture pass or call a longer wait a fix.
4. Any new statistical qualification needs genuinely fresh independent future
   evidence and sufficient per-device coverage. The exposed synthetic fixture
   cannot supply it; do not silently narrow the 200-device experiment.
5. Keep one Draft PR targeting `codex/universal-u05b4b3-granular-retention`.
   Never merge, install, activate neural Control, change the active model,
   relax gate criteria or reuse an exposed holdout as independent evidence.

# U05b.4b.4 continuation handoff

Branch: `codex/universal-u05b4b4-e2e-neural-validation`.
Base: exact PR #363 `e797439989dba56ca2ccf3d37cf1b3521c501b1c`.
Implementation HEAD: `e6d13526f0bf30ab82d35483d24502c9bd5443eb`.
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

Fresh local suite, controlled runtime comparison, required CI and Draft PR
publication are pending final recording. U05b.4b.4 is not yet declared complete.

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

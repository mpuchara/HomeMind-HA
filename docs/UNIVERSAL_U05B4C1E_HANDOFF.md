# U05b.4c.1e continuation handoff

Draft [#367](https://github.com/mpuchara/HomeMind-HA/pull/367) stacks on #366
at `7cf56533187a7db43a0d5d384b8e82444c0108d1`. The measured current runtime is
#366. The new commits change only benchmark tools, tests and reports.
The [preregistered protocol](UNIVERSAL_U05B4C1E_STEADY_STATE.md) and
[results checkpoint](benchmarks/UNIVERSAL_U05B4C1E_SUMMARY.json) separate
matched, natural and diagnostic observations. Prior #366 A–D results
remain in [the previous recovery summary](benchmarks/UNIVERSAL_U05B4C1_RECOVERY_SUMMARY.json).

## Resume the running serial experiments

Start by reading the actual WSL state. The original directory is
`/home/mpuchara/homemind-u05b4c1e`. The primary driver writes one immutable
summary, log and raw archive per case under `primary/`. Its follow-up
coordinator waits for all 24 primary summaries, then runs `extension/` and
validates the small timeline fixture before `diagnostic/`. It records progress
in `continuation.log`. Preserve every timeout and orphaned log. Avoid launching
another timed job while a driver or child is still running.

From WSL in this worktree, the following command continues an interrupted
primary series. Use it only after confirming the original process stopped.
Drop `--resume` and replace the output name for a genuinely new series.

```bash
~/homemind-perf-venv/bin/python tools/benchmark_steady_state_pairs.py \
  --baseline ~/homemind-u05b4c1-recovery/base364 \
  --audit ~/homemind-u05b4c1e/audit \
  --current ~/homemind-u05b4c1e/current \
  --output ~/homemind-u05b4c1e/primary --resume
```

The primary harness must retain the SHA in its `plan.json`. To continue a
stopped follow-up, use the same source flags and the relevant
`--series extension` or `--series diagnostic` plus its output directory.
Use `--resume` if that directory already has a plan. Generate a fresh
checkpoint after the series finishes.

```bash
python3 tools/summarize_steady_state_pairs.py ~/homemind-u05b4c1e
python3 tools/summarize_steady_state_timeline.py <diagnostic-raw.json.gz>
python -m unittest discover -s tests -p test_benchmark_matched_workload.py -v
```

The timeline export separates pre-submission delay, executor queue wait,
worker-to-agent and agent-to-Executor spans. Within these intervals, Store,
SQLite and training spans measure exposure. Interpret truncated traces only
over their retained joins. The diagnostic companion also counts physical
collector rows after shutdown and after the measurement window.

## Remaining acceptance

Gate D stays **INCONCLUSIVE** until the independent matched and natural
series are analyzed. The timeline has benchmark-only instrumentation.
A causal fix requires controlled evidence beyond correlated spans.
Current warmup, count parity and collector results must be assessed across
the full matrix. Live Raspberry Pi and Home Assistant remain untested.

Qualified V2/V3 under load remains a separate benchmark. Begin with the
original `OriginalEvidenceScenario` and the existing threaded collector
qualification test. Its paced waits are a positive control. Add a sustained
case that uses the native writer's real foreground admission and measures
predecision preparation opportunities and misses without forcing production
mask availability. Reuse declarations, source/receipt/provenance interfaces
and native retention. Record source export failures and U05a rejection
reasons separately from SQLite durability. The sparse four-scale benchmark
from #366 does not substitute for this experiment. Pay attention to the
native predecision snapshot's agent limit when constructing the 200-device
scenario with Candidate agents.

Follow exact-head [checks for #367](https://github.com/mpuchara/HomeMind-HA/pull/367/checks)
for Python 3.11, Python 3.13, configured benchmarks and image smoke. The base
[#366 run](https://github.com/mpuchara/HomeMind-HA/actions/runs/41800500067)
only validates the base. Keep all versions and CI identities distinct.

# U05b.4c.1g — continuation handoff

Draft [#369](https://github.com/mpuchara/HomeMind-HA/pull/369) targets
[#368](https://github.com/mpuchara/HomeMind-HA/pull/368) at
`951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`. Use the worktree
`HomeMind-HA-universal-232` and branch
`codex/universal-u05b4c1g-evidence-scalability`.

## Resume U05b.4c.1g.2

1. Use the exact runtime of #368 on an ext4 source snapshot and use the same
   benchmark harness on both sides of each pair. Keep profiling runs separate
   from latency comparisons. Use fresh output filenames and run workloads serially.
2. Investigate a collector-local preparation budget that charges preparation
   work and credits existing idle time. Avoid a blocking sleep in the collector
   writer loop. Recheck foreground admission between bounded turns. Measure the
   single-target policy before changing batch size or snapshot lifetime.
   Exercise admission when an inference future becomes active during a
   previously idle turn. Use the existing exact binding and collector
   persistence tests to check that the runtime still fails closed.
   Above the bound, consider checking a bounded row count in the same
   SQLite transaction before copying tables. Keep revision, snapshot byte
   and table row checks for supported snapshots.
3. Compare Normal at 1000/200 and Busy within 256 durable configurations
   with identical roots, Candidate counts and inputs across revisions. Keep
   Busy with 400 configurations as a separate unsupported control. Compare
   Collector OFF and incomplete modes and verify original intent parity and
   HA call counts. Preserve original timestamps, revision checks and bindings.
4. Continue .3 with Candidate recovery and receipt-fixture stress. Compare
   the same Busy workload with only evidence preparation toggled, then
   stop ingress and measure Candidate drain and a fresh post-overload wave.
   Continue .4 with transport, scheduler and HTTP timing in production composition.
   Use disposable data and a fake WebSocket. Preserve fault tests and report
   the exact CI run for each tested SHA.

In WSL, use `~/homemind-perf-venv`. Local JSONL and logs are under
`~/homemind-u05b4c1g`. The existing seed fixture caps roots at 200. Use
128 roots + 128 Candidates or 200 + 56 for exactly 256 durable configs,
130 + 128 for 258, and default Busy at 200 roots for 400.

```bash
source ~/homemind-perf-venv/bin/activate
python tools/benchmark_qualified_evidence.py --cases 1000:128,1000:130 \
  --modes normal,busy --candidates 128 --fanout 16 --profile-prepare \
  --output ~/homemind-u05b4c1g/mixed-next.jsonl
```

See the [evidence report](UNIVERSAL_U05B4C1G_EVIDENCE_SCALABILITY.md),
[stability report](UNIVERSAL_U05B4C1G_STABILITY.md) and
[machine-readable checkpoint](benchmarks/UNIVERSAL_U05B4C1G_SUMMARY.json).

## Production composition entry points

Reuse the FakeWebSocket from `tests/test_release_117_ha_websocket_id_order.py`
and the native reconnect examples in `tests/test_universal_history_collector.py`.
Compose them with the real Engine and the scheduler workload from
`tools/benchmark_runtime_scaling.py`. For loopback-only HTTP timing use
`GET /api/agents`, `GET /api/candidates` and
`GET /api/agents/{id}/teach-rl-status` through the existing routes.
Record service-call attempts at the transport boundary and compare HTTP
latency during idle, sustained write load and reconnect.
# U05b.4c.1g.2 handoff

Cadence checkpoint `ab7116f585a2016c3a4a2cf6d513a7a62c3b40fb` is the
latest runtime. Its snapshots and raw results are in WSL under
`~/homemind-u05b4c1g/g2cad-ab7116f5-siPzhv`. All four supported Busy
pairs passed the registered margins. Normal passed once and breached
p95 and CPU in the reverse-order pair. Focus subsequent diagnostics on
Normal before changing the preparation cadence again. Compare CPU per
turn and overlap with the next ingress rather than using aggregate CPU
alone. Preserve all observed pairs and the registered margins.

Snapshot optimization `3aecd8182223fa69af4578c6edbc7bbd7a1ae284` is
the latest runtime checkpoint. Use the paired sources, logs, harness
hashes and source identity in WSL under
`~/homemind-u05b4c1g/g2opt-3aecd818-mjmqTW`.
The follow-up cleared all four Busy latency screens and three of four
Busy CPU screens. Normal breached p95, p99 and CPU margins in its
follow-up pair. Use both revisions' data when selecting the next
experiment.

For the remaining cost, first measure Normal with preparation ON and
OFF at the same revision and with identical collector writes. Join
preparation turns to ingress intervals and split CPU between RAM
snapshot copying, SQL, agent normalization and the original arbiter.
Measure attempts per ingress window and the proportion consumed.
Use a fixed, serial Busy diagnostic to separate training/Candidate
completion-count changes from preparation CPU. Repeat the original
natural workload independently for acceptance. Keep the existing
throughput and latency margins.

Implementation `245307f470cd710e0a3d6906934d825573d5a3d8` on Draft #369
was compared with exact baseline `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
Keep both archived sources under
`~/homemind-u05b4c1g/g2-245307f470cd710e0a3d6906934d825573d5a3d8-B28dat`
in WSL. The directory has source identities, harness hashes, raw JSONL,
stderr and exit status for each attempt. The initial archive attempt in
the sibling `-GEC7CU` directory failed before running a benchmark because
Linux Git could not resolve the Windows worktree pointer. Windows Git
archive through WSL produced the two ext4 snapshots for the measured runs.

Start from the g.2 evidence report and the g.2 gate table. Both Busy cases
already have one reverse-order repeat. All four pairs exceeded the CPU
margin and the latency screen was mixed. Profile preparation overlap with
ingress and Candidate activity before changing the preparation policy.
Keep the synthetic background worker's actual update and Candidate counts
visible, since fixed ingress does not guarantee identical background work.
Use unprofiled measurements to assess latency. Keep the 400-config control
and limit the supported comparisons to at most 256 durable configs.

From each snapshot root (`base` and `current`), run the same command with a
fresh output suffix. Use `--cases 1000:200 --candidates 56` for the other
supported Busy case.

```bash
source ~/homemind-perf-venv/bin/activate
python tools/benchmark_qualified_evidence.py --cases 1000:128 --modes busy \
  --candidates 128 --ticks 24 --period .4 --fanout 16 \
  --output "$(mktemp -d "$HOME/homemind-u05b4c1g/g2-extra-XXXXXX")/busy128.jsonl"
```

For g.3 use the same Busy workload with only preparation toggled. Measure
the first queue drain, stale futures and a fresh wave. Preserve the prior
retention failure log and run rollback in both retention-first and
retention-last orders. For g.4 compose the real scheduler with the fake
WebSocket and time the three existing HTTP routes. For g.5 run the
four-hour soak on ext4 with a fresh output name and no concurrent latency
run:

```bash
source ~/homemind-perf-venv/bin/activate
python tools/benchmark_qualified_soak.py --duration 14400 --window 45 \
  --output ~/homemind-u05b4c1g/soak-4h-final.jsonl
```

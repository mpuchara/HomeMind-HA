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

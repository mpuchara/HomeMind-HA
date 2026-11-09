# Foreground drain deadline recheck

The candidate allows one immediate return to normal admission after a foreground writer turn crosses the protected window. The recheck requires pending evidence, open foreground eligibility, zero preparation debt, no retry cooldown and a running collector. The next loop performs the normal writer drain and preparation checks again. New foreground activity can still block preparation at that boundary.

The controlled differential uses the collector loop frozen from `9c245577`. It advances an injected clock across the foreground deadline during a drain. The original loop polls for 250 ms and prepares at 580 ms. The candidate returns to normal admission and prepares at 330 ms. The budget-debt, active-foreground and retry-cooldown controls keep the original call sequences. A second differential compares traced and untraced preparation turns in six cases, including no-op and exception debt accounting.

[Traced scheduling divergence](FINAL_RELEASE_EVIDENCE_SCHEDULING_ROOT_CAUSE.md)


## Unprofiled A/B screen

Candidate `b141ac0fe9033550c5ee03d0a4eb2ccaef5a9541` has add-on tree `8ebe020211e829b8b80a82b9dabdae3775624d5d`. Its frozen WSL source passed 2 scheduler tests, 295 Universal tests and 2 exact receipt writer differential tests. The screen compares it against retained runtime `9c245577727c93ede3c59100f11844bf336acb1a` with 24 ticks, 400 ms and configured fanout 16. CPU, p95, p99, RSS and work counts below compare retained → candidate.

| Pair | CPU s | p95 ms | p99 ms | RSS MB | Consumed / qualified | Writer batches | SQLite deferrals | Screen |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| small50-1 | 0.391 → 0.423 | 7.50 → 8.58 | 31.67 → 33.27 | 61.55 → 65.85 | [175, 175] / [175, 175] | 53 → 51 | 1 → 3 | FAIL |
| small50-2 | 0.410 → 0.409 | 7.24 → 7.46 | 29.74 → 36.75 | 60.96 → 65.80 | [166, 175] / [166, 175] | 55 → 52 | 0 → 2 | FAIL |
| normal-1 | 1.852 → 1.610 | 80.86 → 61.37 | 102.08 → 69.16 | 79.49 → 79.54 | [13, 13] / [13, 13] | 154 → 141 | 0 → 0 | PASS |
| normal-2 | 1.620 → 1.607 | 61.43 → 60.73 | 71.43 → 69.13 | 78.88 → 79.55 | [13, 13] / [13, 13] | 145 → 138 | 2 → 1 | PASS |

All four pairs preserve inputs, original decisions and native accounting, with zero event/Collector drops, Collector errors or HA calls. Both Normal screens pass and qualify 13 in each arm. The first 50/10 pair fails p95 at +14.42%. The reverse 50/10 pair fails p99 at +23.57% while qualifying 166 → 175. These screens show mixed end-to-end results. Revert `267ec76b365b5a038b67062df6f75e1498c1a965` restores add-on tree `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`. The 13-pair acceptance protocol against #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d` awaits repeatable improvement. Gate D remains **BLOCKER**.

Checkpoint CI [42666974689](https://github.com/mpuchara/HomeMind-HA/actions/runs/42666974689) succeeded on reporting `9d6bbfa9`. The candidate results above are local WSL validation. Reporting CI for the new trace tools is tracked on Draft PR #369.

## Controlled native replay

An explicit diagnostic pause recreates the crossing after a successful foreground drain at tick 12. It runs after the original drain returns, when its write queue is empty and inference futures are complete. The pause lasts until the current priority deadline plus 24 ms. The original workload stream and evidence clocks run through their existing paths. Each of the four arms records one injected pause.

Both alternating pairs recover one receipt, **12 → 13**, with all 384 original decisions, matching signatures and zero event or Collector drops. Candidate selections of `light.fixture_12` occur at 5130.35 and 5131.70 ms relative to first ingress. The retained arms take the periodic wait instead. This reproduces the mechanism under a synchronized delay. The earlier unprofiled 13 → 11 run remains unattributed at the scheduling-event level.

The controlled recovery is repeatable. The unprofiled 50/10 screens still fail their timing limits, so the experiment remains reverted.

[Raw traces and replay](benchmarks/evidence-scheduling-20261109/raw.tar.gz) · [SHA-256](benchmarks/evidence-scheduling-20261109/raw.tar.gz.sha256) · [Source integrity](benchmarks/evidence-scheduling-20261109/source-integrity.json)

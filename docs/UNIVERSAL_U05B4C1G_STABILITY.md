# U05b.4c.1g — stability and acceptance

**Overall status is BLOCKER.** This checkpoint covers preparation diagnosis
using the production evidence code and a synthetic load driver.

| Gate | Status | Scope |
| --- | --- | --- |
| A · Functional safety | INCONCLUSIVE | Synthetic intents and zero HA calls in this matrix. Full matched runtime comparison is pending. |
| B · Warmup | NOT RUN | The qualification matrix seeds the scenario before timing. |
| C · Collector durability | Observed in synthetic load | 384/384 original decisions in each baseline repeat with zero collector drops. |
| D · Steady-state performance | INCONCLUSIVE | Host profiles are diagnostic. Matched regression study is pending. |
| E · Qualified V2/V3 evidence | BLOCKER | Low yield below the configuration bound. |
| F · Predecision scalability | BLOCKER | Budget waits and pending backlog within the bound. Above 256 durable configs is UNSUPPORTED. |
| G · Long-term stability | NOT RUN | Four-hour and eight-hour variants pending. |
| H · Production composition | NOT RUN | Full scheduler, WebSocket transport and HTTP measurements pending. |
| I · Raspberry Pi / real HA | NOT RUN | Disposable synthetic host only. |

The earlier isolated retention rollback miss
`missing_historical_decision_receipt` remains **INTERMITTENT / UNRESOLVED**.
The rollback test passed in the focused suite and in ten isolated runs
in fresh processes. Test order, cleanup and timing remain possible factors
in the earlier miss. Preserve the aggregate failure from #368 when collecting
additional logs.

The component soak retained one synthetic Engine and Store for three windows.
It finished in 12.677 s with exit 0, zero threshold failures and zero HA
service calls. It exercises collector reconnect and SQLite busy. The runner
reported **INCONCLUSIVE** for stability because the duration gate was not met.
Fault coverage for the closed SQLite session and delayed metadata commit
belongs to the existing collector tests. End-to-end HTTP latency and UI
rendering are distinct measurements.

## Focused validation

The WSL Python 3.12 focused suite passed 291 tests in 165.443 s, including
predecision, affected-sensor, historical collector, retention, native data
and offline lifecycle tests. The collector regressions for a closed session
and delayed metadata commit passed in this suite. This result applies to
the ext4 source snapshot with the benchmark instrumentation applied.

See [instrumentation commit CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/41892759549)
for the separate Python 3.11, Python 3.13 and image results.

## g.2 gates for implementation `245307f4`

**Overall status is BLOCKER.** The matched harness provides synthetic
component evidence. The Busy latency and CPU screens did not clear.

| Gate | Status | Scope |
| --- | --- | --- |
| A · Functional Safety | INCONCLUSIVE | Same 384 original intents and signature per matched case. Zero HA calls. Production composition pending. |
| B · Warmup | NOT RUN | Preseeded timed workloads. |
| C · Collector Durability | PASS in matched host matrix | Every enabled row set persisted 384 original decisions with zero collector drops and errors. |
| D · Steady-State Performance | BLOCKER | Busy 128+128 breached both latency margins. Both supported Busy cases breached the CPU margin. |
| E · Qualified Historical Evidence | PASS in matched host matrix | V3 rose 16/1152 → 53/1152. OFF, incomplete and unsupported controls yielded zero V3. |
| F · Predecision Scalability | BLOCKER | Throughput screen passed, but Busy cost remains open. Over 256 durable configs remains unsupported. |
| G · Candidate Recovery | NOT RUN | Load-end diagnostics only. Drain and fresh wave remain open. |
| H · Long-Term Stability | NOT RUN | Four-hour run pending for this SHA. |
| I · Production Composition | NOT RUN | Real scheduler, fake WebSocket, reconnect and HTTP timing pending. |
| J · Raspberry Pi / Real HA | NOT RUN | Synthetic host runs only. |

The retention miss remains INTERMITTENT / UNRESOLVED. The earlier failure
and isolated repetitions retain their original source identities.

On implementation `245307f470cd710e0a3d6906934d825573d5a3d8`, the
ext4 focused suite passed 114 tests in 71.002 s. It covered controlled
budget accounting and foreground admission, detached predecision, the
durable configuration bound, collector contention and restart, historical
retention and affected sensors. The implementation run passed full Python
3.11 and 3.13 regression, the required benchmarks and image build/smoke.
[Implementation CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/41917914493)

### Snapshot follow-up

`3aecd8182223fa69af4578c6edbc7bbd7a1ae284` passed 106 focused tests
in 108.094 s on WSL ext4. Seven new matched pairs preserved 384 intents
and their signatures and recorded zero HA calls and collector drops.
Gate D stays **BLOCKER**. All four Busy pairs met the latency margins,
one breached the CPU margin, and Normal breached CPU and both latency
margins. The supported cases retained the throughput gain. Gates G–J
remain unrun and the earlier intermittent retention miss remains open.
[Snapshot follow-up CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/41922738919)

### Preparation cadence follow-up

`ab7116f585a2016c3a4a2cf6d513a7a62c3b40fb` passed 115 focused WSL
tests in 76.385 s. Nine matched pairs preserved intent signatures with
zero HA calls and zero collector errors or drops. All enabled cases
persisted 384 original decisions. OFF, incomplete and the 400-config
control yielded zero V3. Gate D remains **BLOCKER**. The four supported
Busy pairs and first Normal pair met the registered performance margins.
The second Normal pair exceeded p95 and CPU. The registered throughput
screen passed. Gates G–J remain unrun for this checkpoint.
[Cadence CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/41931141100)

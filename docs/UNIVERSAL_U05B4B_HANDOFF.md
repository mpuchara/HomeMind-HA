# U05b.4b handoff

## Scope and branch

Work resumed on the existing clean local branch
`codex/universal-u05b4b-causal-evidence`, starting at PR #360 HEAD
`72487b945d521c8564928cea770de73023b82136`. No previous implementation or
uncommitted changes existed. Reference remains 0.14.232 /
`f7384ba5476f26155ecb138034cc3350fa5bfda2`. No merge, package, installation,
collector enablement, automatic training or model promotion is authorized here.

This change implements U05b.4b.1 only. U05b.4b.2 affected-sensor evidence,
U05b.4b.3 granular retention and U05b.4b.4 validated offline-artifact end-to-end
acceptance remain outstanding. Production samples with unknown affected sensors
remain ineligible; no empty dependency set is fabricated.

## Implementation

The existing Engine policy actions are authoritative. Before `policy.predict`,
Engine may obtain an already prepared receipt using RAM comparisons only. The
receipt is bound to the actual ActionIntent after prediction. Neither operation
evaluates an arbiter or touches SQLite; absence/failure never changes the normal
policy, intent or Executor path. Executor itself is unchanged.

An idle collector turn prepares at most one requested target. It detaches the
engine registry, states and manual holds, takes one consistent native SQLite
read snapshot of arbiter inputs, and executes the ORIGINAL
DeviceAgentService.descriptor/legal_action_mask over a bounded in-memory copy.
Even the arbiter's existing manual-hold materialization writes only that copy.
There is no new live resource reservation, live manual-hold write or HA action.

The receipt includes agent/config hash, generation, target/property, full policy
action values, original descriptor/mask/resource keys, device/sibling state,
registry revision, collector topology/epoch, native arbiter revision, original
creation and availability clocks, validity deadline and exact decision binding.
No executor mask is relabelled as predecision evidence. Prepared masks become
available before inference; clocks are never backdated.

RAM matching checks agent configuration, exact policy action values, target and
descriptor-related states, registry revision, manual holds and collector epoch
at both boundaries. Original RAM-first Store manual-hold metadata is detached
and compared too: fresh SQL metadata must not silently replace a different
process-local cache input. Engine/context snapshot locks are acquired without
waiting behind foreground work. Validity also ends at the earliest pending lease/hold/dwell
transition, with a maximum five-second cache age. A timestamp alone never proves
arbiter freshness.

INSERT/UPDATE/DELETE triggers on agents, explicit mappings, resource state,
metadata and generation lineage advance a durable revision transactionally,
including writes through another connection/process. The collector takes a
writer transaction and checks the prepared revision before persisting its
assertion. This deliberately rejects ANY covered write since preparation,
including unrelated writes and writes after the decision but before persistence.
The check does not delay control. Later revisions do not erase an assertion
already correctly persisted. Missing tables/triggers/generation fail closed;
restart never resurrects a RAM witness.

Preparation is admitted by the existing foreground-idle and TRAINING_BUDGET
rules. Limits: 256 agents/pending/prepared slots; 256 complete actions (no
subsampling); 10,000 rows/table; 4 MiB total copied SQL inputs and engine snapshot;
262 KiB/evidence; 8 MiB prepared cache. Overflow or unsupported scale means no
evidence. Sensor history remains exclusively FeatureJournal.

Generic number/input_number/select/input_select targets explicitly remain
unsupported for detached evidence: their original legal_value review reads
global storage.STORE rather than the service's store. Such targets are rejected
before mask evaluation, never evaluated against later live review metadata or
enabled by patching the production singleton. Their control path is unchanged.
Extending that original review's immutable-input contract is follow-up work.

U05a validates the optional embedded evidence, mask completeness and exact
identity/config/generation binding. The collection adapter checks native
provenance and generation and binds the embedded receipt to the persisted
assertion. Existing explicit historical receipt format remains supported.
The production caller still supplies `affected=None`, so the mask can be retained
independently while the training receipt remains absent. Existing power/lux
exclusions and global retention witnesses are unchanged.

## Validation status

Implementation: `768ec40fde6abb15190bb4623d269448150fb11e` (the later handoff
commit contains documentation/measurement receipts only). Draft PR:
https://github.com/mpuchara/HomeMind-HA/pull/361, base
`codex/universal-u05b4a-history-collector`. Focused predecision tests: **14
passing** on Windows Python 3.11; existing collector tests: **26 passing**.
The complete Universal subset on WSL Python 3.12.3/NumPy 2.3.3: **204 tests,
49.068 s, OK**. Compileall passed. Native integration uses actual wall
clock order, original mask, original ActionIntent, original FeatureJournal seeds, native Correct and
provenance, NativeDatasetSource/U05a, plus Undo. Explicit affected-set input in
that fixture is a caller-supplied test contract, not a claim of production
dependency inference. Missing/expired/partial/unverified evidence rejects data.
The real Engine.process_agent regression verifies that evidence is consumed
before the original policy predict, its id matches the original ActionIntent,
and a second pass without evidence still submits exactly one unchanged proposal
with zero HA service calls.

Full Windows discovery did not complete: the verbose attempt stops in
`test_release_080_training_process...test_parent_process_identity_uses_pid_plus_start_token`,
with earlier errors in the existing `test_release_032_discovery_qos` cases.
There is no full Windows pass claimed. Its logs remain locally in
`.devtools/u05b4b-full-tests.log`. The existing composed benchmark also fails its
Windows temporary-SQLite cleanup before returning a result; this is not accepted
as a successful latency run. Linux CI is the full cross-version verification.

Initial implementation `817a592afe74a7a4bbc75d7d2b979837ed81db13` completed
[full CI 41611043577](https://github.com/mpuchara/HomeMind-HA/actions/runs/41611043577)
successfully (image and Python 3.11/3.13 jobs; package skipped). Final implementation
CI [41612630129](https://github.com/mpuchara/HomeMind-HA/actions/runs/41612630129)
is **completed / success** for exact implementation commit `768ec40f`:
Python 3.11 **3028 tests / 211.428 s / OK**, Python 3.13 **3028 tests /
310.169 s / OK**, all required benchmark/entrypoint steps and image smoke passed;
package skipped. The documentation/receipt-only follow-up triggers a separate
automatic CI run; do not confuse its status with this verified implementation.
U05b.4b.1 is implemented and host-validated within the support/acceptance limits
documented here. This does not complete U05b.4b.2–4 or Pi4 acceptance.

## Host performance receipts

The exact predecessor is PR #360 `72487b945d521c8564928cea770de73023b82136`,
collector enabled for both A/A controls. `UNIVERSAL_U05B4B_IMPACT_HOST.json`
records the final measured runtime revision
`357cec3baf5e0176e3836a312435ef7f37496c53`. The later implementation follow-up
tightens U05a historical clock/topology/contract validation; results are not
silently relabelled as a different measured commit. WSL Ubuntu, Python 3.12.3,
NumPy 2.3.3; 1000 entities, three real policies, 20 warmup and 120 measured
events, 360 intent latency samples. Event clocks are the existing identical
synthetic runtime benchmark inputs; scheduling/latency/CPU clocks are real.
The native training integration above uses real receipt clocks instead.

| Case | event-to-intent p50 ms | p95 ms | p99 ms | process CPU ms | RSS MiB |
|---|---:|---:|---:|---:|---:|
| PR #360 on A | 3.822 | 7.256 | 8.949 | 938.9 | 74.70 |
| Current off | 3.662 | 6.712 | 9.576 | 910.9 | 72.12 |
| Current on A | 3.671 | 6.544 | 7.930 | 894.1 | 73.13 |
| PR #360 on B | 3.489 | 6.942 | 8.690 | 900.7 | 74.02 |
| Current on B | 3.891 | 6.941 | 8.463 | 950.6 | 73.99 |

All five input and decision hashes are identical; **zero HA calls**. Each enabled
case writes 560 collector receipts with zero dropped receipts/errors. Collector
write p95: predecessor 973/980 ms, current 968/1196 ms. Deferred shutdown/backlog
latency is not event-to-intent latency. The slower CPU and collector-latency case
is retained. The initial series is also retained: predecessor intent p95
8.47/6.26 ms versus current-on 8.42/8.60 ms. These distributions do not establish
zero overhead or uniform improvement. 120 events are insufficient for robust
production p99. See JSON and the five final raw logs for cache/SQLite diagnostics,
RSS/peak RSS, pipeline quantiles, losses, CPU, complete decision rows and clocks.

The busy composed benchmark defers all preparation (`prepared=0`, `consumed=0`)
and measures the cold/missing-evidence path. It does **not** establish active
preparation/consumption performance under production contention. The real Engine
regression separately proves consumption and one-intent/zero-HA behavior.

`UNIVERSAL_U05B4B_PREDECISION_HOST.json` measures original detached arbiter
preparation and RAM bindings at all requested cardinalities, with SQL tracing:

| Sensors/devices | Original preparations | Bound receipts | CPU s | RSS after MiB | Native SQL statements | Prepared cache bytes |
|---|---:|---:|---:|---:|---:|---:|
| 5/1 | 1 | 5 | .003 | 31.38 | 13 | 22,340 |
| 50/10 | 10 | 50 | .024 | 31.52 | 130 | 260,480 |
| 200/30 | 30 | 150 | .110 | 32.18 | 390 | 1,048,160 |
| 1000/200 | 200 | 1000 | 2.367 | 37.46 | 2600 | 8,285,400 |

Every measured RAM binding asserts that it performs no SQL. Its reported
boundary timings include assertion-mock setup and must not be substituted for
Engine latency. At 1000/200 the preparation wall p95 is 262 ms, including
cooperative TRAINING_BUDGET waits; CPU is reported separately. The worker is
deliberately allowed to yield or miss evidence rather than compete with control.
There are zero additional HA calls/intents and **zero training-eligible production
samples**, because affected sensors remain unknown. The single eligible native
test sample uses an explicitly supplied independent fixture dependency contract
and becomes ineligible after Undo.

`UNIVERSAL_U05B4B_COLLECTOR_HOST.json` retains the four Windows collector
component cases: 256 original sensor observations/case, zero dropped events or
write errors. At 1000/200: 1.484 CPU s, 6.986 wall s, 4,943,872 bytes of database
growth, 4,427,777 traced Python peak bytes. This early series may overlap full
test discovery; traced Python allocations are not RSS. The initial Windows
predecision report is also retained and explicitly marked as incomplete harness
coverage (only its first target was warmed), superseded by the Linux report.

No full composed 1/10/30/200-device latency sweep or active-collector contention
gate is claimed. These remain acceptance work with actual Pi4/HA/socket/ACK/soak
measurements. Host component checks are not hardware acceptance.

## Continuation

1. Read this handoff and verify current branch/PR HEAD and its live CI before
   continuation. Preserve the Draft stack and the validated .1 implementation.
2. Review conservative postdecision revision rejection and idle-only coverage;
   high write rates may yield no evidence. No stale evidence may be admitted to
   increase yield. Retained SQL triggers still increment revisions if a later
   boot disables collection; they contain no history or callback dependency.
3. For .2 add an explicit versioned complete affected-sensor contract. Unknown
   remains None. Preserve power/lux exclusions until independently supported.
4. For .3 extend native FeatureJournal retention witnesses atomically; no second
   sensor archive. Legacy witnesses must fail closed if unverifiable.
5. For .4 extend the native real-clock fixture through the existing offline
   trainer/validator/lifecycle. Current fixture ends at TrainingDataset/Undo;
   there is no claimed validated neural artifact from production data.
6. Repeat composed runtime benchmarks under actual Pi4/HA contention and evaluate
   losses, qualification, SQLite load, RSS, CPU and p50/p95/p99. Current host
   results cannot establish no production regression or hardware acceptance.

# U05b.4a: historical training data collector

## Checkout and references

Worktree: `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`.
Branch: `codex/universal-u05b4a-history-collector`.
Draft PR: [#360](https://github.com/mpuchara/HomeMind-HA/pull/360), targeting
`codex/universal-u05b3-artifact-lifecycle`, never main.
Last implementation commit: **7196b9e37effd074391dda017aad8c2eb574f0fb**.
Documentation/evidence-only commits may follow; inspect `git rev-parse HEAD`.

Predecessor #359 HEAD **3c3ccee8354cc74aab8ac871a010883b7054cbf1** and its
[successful final CI 41554020141](https://github.com/mpuchara/HomeMind-HA/actions/runs/41554020141)
were verified live before branching. Its base is #358 at `6a9beaa3`.
The stacked Draft chain #355 -> #356 -> #357 -> #358 -> #359 -> #360 remains
unmerged. Production reference stays **0.14.232** /
`f7384ba5476f26155ecb138034cc3350fa5bfda2`. No install package or version bump.

Read `UNIVERSAL_U05B3_HANDOFF.md`, `UNIVERSAL_U05A_TRAINING_DATA.md` and
`UNIVERSAL_U05_DATA_SOURCE_AUDIT.md` before continuing.

## Implemented production collection

`universal_history_collector.HistoricalTrainingCollector` is installed through
the shipped RuntimeComposition root only when
`universal_training_collector_enabled: true`. Both config schema and Python
defaults set it to **false**. Disabled startup creates no collector worker,
mailbox or collection tables. Enabling records local evidence only; it does not
start neural observers, training, Candidate creation, promotion or model updates.

One bounded mailbox has 1024 items and an 8 MiB conservative payload charge;
each writer batch has at most 64 items. Producers detach bounded JSON-shaped
data and take a short mailbox lock. They perform no collector SQLite operation,
capacity wait, HA request or new legal-mask evaluation. Oversized or unsupported
payloads become gaps. Limits include 10000 entities, 4 MiB snapshot/registry
receipts, 256 KiB decision/mask copies, structural depth 10 and 65536 nodes.
These are input/work bounds, not a promise that RSS equals encoded byte size.

Sensor values go exclusively to **native FeatureJournal** with native observation
contract version, state, event/received clocks, unit, device class, state class,
options, availability encoded by unknown/unavailable state, and quality.
Live events reuse native `ha_state_changed` stable event keys, including HA
context ids, so watched events are not duplicated into another sensor history.
Full snapshots are new observation receipts of the existing value, retaining
the original HA event time. Archive recording continues through its existing
queue and housekeeping; no synchronous `flush_archive()` was added to REST/WS.

Sidecar tables hold collection sessions, gaps, combined HA registry versions,
decision receipts, original mask receipts and coverage segments. They do not
replace provenance or create another Correct journal. Entity/device/area lists
are detached and versioned with content checksums and acceptance clocks. Missing
or duplicate registry identities invalidate collection; today's registry never
repairs an older decision. Historical device descriptions are preserved inside
the original mask receipt when that existing call actually produced one.

The Engine hook records the existing intent id, exact timestamp, agent config,
target/property, policy/model revision, target state and sensor dependencies.
Known affected-sensor sets are explicit; production currently records the
mapping as **unavailable**, not a fictional empty set. Generation and execution
facts stay authoritative in native provenance, keyed by that intent id.

`native_decision_evidence(conn, id)` reads existing durable facts without forcing
writer flushes. It distinguishes model prediction, chosen value, existing
ActionIntent, dispatch status/time, ACK time/id and the correlated state event.
ACK correlation is not independent actuator telemetry: physical execution
confirmation remains unknown. Shadow prediction never becomes dispatch evidence.

`DeviceAgentService.legal_action_mask` returns its original result unchanged.
The optional tap copies that exact result and descriptor after the existing
evaluation, recording contract versions, original guard clock, information
availability time and stage (`executor_guard` or `unlinked_observation`).
No extra mask/descriptor query, ActionIntent or Executor call is made.

## Completeness protocol

Positive coverage needs a confirmed WS session, complete accepted versions of
all three registries, every expected enabled sensor in a full snapshot, and a
REST **request start** after subscription and topology acceptance. The request
epoch must still match on response. An old request crossing a gap cannot open
a new complete interval. Rejected snapshots have durable diagnostic markers.

Coverage advances only on actual received state frames after their producer
work is enqueued. Socket receive timeouts do not manufacture watermarks.
Disconnect, new socket session, registry notification, missing state/clock,
malformed frames, overflow and write failures end eligibility. Registry notices
use the existing coalesced, monotonic-id, bounded HA read-request machinery to
refresh entity/device/area evidence. These are read-only metadata requests.
Stale in-flight registry responses cannot qualify a new epoch. Coalesced errors
retain the strongest registry invalidation rather than erasing its cause.

On failure, an out-of-band gap latch survives mailbox exhaustion. The writer
closes the current segment at its last persisted trustworthy watermark. A fresh
complete snapshot is required to restart coverage; some errors also require
fresh registry lists. Restart does not trust pending RAM data. Indexed startup
examines at most 32 open sessions and marks interrupted sessions/segments;
it never scans/replays sensor history to rebuild RAM state.

Native journal rows are committed before their coverage proof. Proof bodies and
watermarks have separate checksums. A partial batch or process exit before proof
insertion cannot authorize incomplete history. This is tested with `os._exit`,
not just an exception that runs Python cleanup. SQLite/process recovery tests
are not a physical power-cut/storage-controller acceptance certificate.

## U05a/U05b.3 adapter and retention

`source_from_collection(conn, agent_ids, start=..., through=...)` requires a
caller-owned read transaction. It selects one wholly covering verified segment,
checks topology/decision/proof checksums and current retention, and returns the
existing `NativeDatasetSource`. Supplied historical decision receipts must also
match native provenance agent, timestamp and model revision. No fuzzy timestamp
matching, current-registry reconstruction or pending-provenance flush occurs.
Missing receipts remain explicit U05a rejections.

ReplayCoverage **v1 retains its previous behavior**. V2 adds exact initial seed
receipts and a native collector retention witness. This permits an initial
state with an old event time without falsely backdating completeness. Both
clocks remain causal; only those attested old seeds can precede coverage start.
Reads retain existing row/byte/sample limits and reject missing/expired seeds.
V2 uses the attested FeatureJournal stream; archive/import rows cannot silently
repair a missing collector event or enrich its exact historical payload.

`prepare_dataset()` verifies the native collection witness on **every use**,
including an earlier exported source later used by the U05b.3 lifecycle. Native
FeatureJournal pruning increments the witness in the **same deletion
transaction**. Tracking remains installed after a later boot with collection
disabled, so disabling the worker does not make stale proofs reusable.
Undo, conflicts, causal replay, event identity, supervision revision, generation
deduplication, excluded affected/power/lux inputs and quality gates are unchanged.

Metadata housekeeping reuses Engine's existing housekeeping and storage budget.
It targets 14-day retention in bounded deletes (128 rows/table and 32 orphan
topologies/sessions per pass), with time/agent/session/topology indexes.
Native FeatureJournal retention and hard caps remain authoritative.

Foreground futures and the existing realtime priority duration defer collector
writes/maintenance. Existing TRAINING_BUDGET checkpoints run before transactions
and between native batches. Continuous activity may cause bounded overflow and
explicit gaps; collector data must yield to Control/UI. Shutdown drains after
producers stop, with a bounded join; an unfinished session is rejected on restart.
Optional collector initialization/maintenance errors cannot abort runtime startup
or the native housekeeping chain. Queue/error/latency/deferral diagnostics appear
in the existing runtime snapshot without disk reads.

## Important remaining boundaries

* **Normal production decisions still lack a true predecision full legal mask.**
  Executor generally evaluates one selected value after intent creation. It is
  archived faithfully but never converted to earlier authority. Missing causal
  masks and unavailable affected-sensor mapping keep those samples ineligible.
  The trusted explicit `HistoricalDecision` receipt interface is exercised by
  fixtures; do not fill it by recalculating a mask later. Production collection
  does not imply that all recorded decisions are now trainable.
* Retention invalidation is deliberately global/conservative. Deleting unrelated
  native rows may invalidate otherwise intact segments. Refine deletion witnesses
  per window/entity before claiming high long-term training-data availability;
  never bypass the witness or lower quality requirements.
* The adapter reads one segment and explicit actor ids. It does not stitch across
  gaps/topology epochs, guess missing generation receipts or solve the existing
  2048-row/per-device support limit. Missing lineage context fails closed.
* Cold startup may wait for the next existing full poll if bootstrap REST began
  before WS/registry readiness. No extra forced REST resync is introduced just
  to fabricate collection coverage. Quiet periods do not advance from a timer.
* Physical Pi4 CPU/RAM/UI, actual HA sockets/dispatch/state-ACK and long soak
  acceptance are still open. Keep the option disabled for production acceptance
  until those gates and the missing proof producers are addressed.

## Validation and reproducible commands

Final focused collector suite: **26 tests passed in 12.761 s**. Covers all four
cardinalities, actual native event-key reuse, topology movement, connection loss,
request/response epoch boundaries, malformed data, coalesced invalidations,
overflow, actual abrupt process exit, corruption, retention after disabling,
U05a preparation plus real Undo, original mask capture, native Shadow provenance,
startup isolation and foreground deferral with clean drain.

Earlier complete local Linux receipts: 3005 tests / 392.784 s, 3010 / 388.544 s,
3011 / 369.915 s, all OK/exit 0, before subsequent focused safety/admission changes.
Use final-commit CI below for final code rather than attributing old runs to HEAD.

Final implementation CI:
[41580614869](https://github.com/mpuchara/HomeMind-HA/actions/runs/41580614869).
The previous idle-admission implementation also completed full successful CI
[41579138755](https://github.com/mpuchara/HomeMind-HA/actions/runs/41579138755).
Final implementation CI **completed successfully**: Python 3.11, Python 3.13,
image build/smoke and all required benchmarks, including cardinality collection
and enabled collector + Candidate/training/Correct smoke. Package was skipped.
The following documentation-only commit has its own automatic CI; do not
attribute this implementation run to a different executable-code SHA.

| Final evidence | Result |
| --- | --- |
| CI Python 3.11 | 3014 tests, 282.005 s, OK |
| CI Python 3.13 | 3014 tests, 304.889 s, OK |
| Image and all required benchmarks | success |
| Installation package | skipped |

The final native benchmark copy matches the Windows worktree:

* Collector SHA256: `99b5ae0c9cf1e222a5399546348ff61281c2860eb465050cb52533de592d8e4b`.
* Engine SHA256: `701f143747debf3a77ae37205accf093a713261a752fd3348463ee36f311d493`.

Checked-in receipts under `docs/benchmarks/`:

* `UNIVERSAL_U05B4A_COLLECTOR_HOST.json`: final idle cardinality/cost run.
* `UNIVERSAL_U05B4A_IMPACT_HOST.json`: final separate runtime comparison.
* `UNIVERSAL_U05B4A_IMPACT_INITIAL.json`: slower initial implementation, retained.
* `UNIVERSAL_U05B4A_IMPACT_COOPERATIVE.json`: intermediate cooperative admission,
  still showing interference before foreground-idle admission was added.
* `UNIVERSAL_U05B4A_IMPACT_CONTENDED.json`: explicit concurrent-local-suite series;
  retain it as contended evidence, not an isolated performance comparison.

Final separate runtime measurements (milliseconds):

| Case | event-to-intent p95 | outer pipeline p95 | active-window CPU ms | RSS MiB |
| --- | ---: | ---: | ---: | ---: |
| #359 base A | 7.36 | 8.42 | 949.12 | 72.32 |
| Current, collector disabled | 6.59 | 7.44 | 901.62 | 71.49 |
| Current, collector enabled A | 6.58 | 7.37 | 902.75 | 74.19 |
| #359 base B | 5.96 | 6.71 | 837.38 | 72.51 |
| Current, collector enabled B | 7.33 | 8.16 | 945.06 | 74.51 |

All five input and decision hashes match exactly, with zero HA service calls.
Enabled p95 falls within observed baseline A/A spread in this host run; this is
not proof of zero overhead or Pi4 equivalence. Writer work was deliberately
moved out of the foreground window, not eliminated. The producer copy/queue
still has a bounded CPU cost and sustained traffic can sacrifice completeness.

Final cardinality mixed workload:

| Sensors/devices | Wall s | CPU s | Python traced peak bytes | SQLite growth bytes | write p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 5 / 1 | .629 | .285 | 6726755 | 81920 | 242.24 |
| 50 / 10 | 1.469 | .087 | 218705 | 122880 | 247.69 |
| 200 / 30 | 1.023 | .134 | 753290 | 323584 | 263.70 |
| 1000 / 200 | 3.925 | .842 | 3625065 | 4943872 | 357.74 |

Queue high-water mark is 32 items in these explicitly batch-drained workloads;
all four cases finish without drops/errors. Sensor enqueue rate is about
20.6k-25.1k/s and is **not sustained SQLite throughput**. Each case also writes
initial sensor seeds and one receipt per device. Tracemalloc measures Python
allocations, not total/native RSS. First-case peak includes cold adapter imports.

```powershell
python -m unittest discover -s tests -p test_universal_history_collector.py -v
python tools/benchmark_universal_collector.py --output collector-host.json
```

Full Linux tests use a refreshed native ext4 copy:

```sh
cd /home/mpuchara/homemind-universal-u05b4a/current
/home/mpuchara/homemind-universal-232-validation/venv/bin/python -m unittest discover -s tests -v
```

Paired runtime command (Linux; exact baseline from `git archive 3c3ccee8`):

```sh
python tools/benchmark_universal_collector_impact.py \
  --baseline-source /home/mpuchara/homemind-universal-u05b4a/base-359 \
  --output impact.json
```

This runs baseline A/A, current-disabled, current-enabled twice through shipped
Root/startup/lineage and normal policies, 1000 entities, 3 agents, 20 warmup plus
120 measured events. It retains input/decision hashes, real CPU/RSS, measured
event-to-intent and outer pipeline p95, collector stats and zero-HA-call checks.
Active Candidate + concurrent training + Correct collector smoke is also in CI;
every existing required benchmark and image check remains enabled. Package is
skipped on this branch. Host evidence is not Pi4/physical HA acceptance.

Persistent native logs/receipts: `/home/mpuchara/homemind-universal-u05b4a/logs/`.
Keep initial slower and contended series as well as final quiet measurements;
do not present producer enqueue rate as sustained SQLite throughput. Cardinality
workloads include initial seeds, 256 events and one receipt/device; the first
case also includes cold offline-adapter imports. Active-window runtime CPU
excludes later shutdown drain; total collector cost is measured separately.

## Continuation

1. Verify #360 HEAD and live CI, #359 dependency and production reference before
   editing. Keep the Draft stack; never use old main/0.14.184 as the base.
2. Review the proof and retention limitations above. Collectors are passive;
   do not enable training/promote models from raw audit rows or post-intent masks.
3. If evaluating on a disposable HA/Pi installation, enable only the collector
   option and inspect runtime diagnostics, gaps, storage pressure and shutdown.
   Obtain genuine fresh WS/registry/request-start/seed evidence before exporting.
4. For offline export: begin Store read transaction, call `source_from_collection`,
   preserve `source.raw`, then use the existing `source.dataset(conn)`/U05b.3
   lifecycle with its fresh read transaction and native supervision. Retention
   witnesses remain mandatory; absent context is a rejection, not a repair task.
5. Prioritize genuine existing predecision-mask/affected-sensor receipt ownership,
   granular retention availability and repeated Pi4/HA/soak/performance acceptance.
   Do not add mask SQL to ingress/inference, a second Correct system, automatic
   training/promotion, version changes, main merges or install packages.

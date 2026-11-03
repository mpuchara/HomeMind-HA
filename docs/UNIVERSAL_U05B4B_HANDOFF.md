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
at both boundaries. Validity also ends at the earliest pending lease/hold/dwell
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

U05a validates the optional embedded evidence, mask completeness and exact
identity/config/generation binding. The collection adapter checks native
provenance and generation and binds the embedded receipt to the persisted
assertion. Existing explicit historical receipt format remains supported.
The production caller still supplies `affected=None`, so the mask can be retained
independently while the training receipt remains absent. Existing power/lux
exclusions and global retention witnesses are unchanged.

## Validation status at implementation commit

Implementation commit and final CI/performance results will be recorded in the
validation follow-up. Focused predecision tests: 11 passing on Windows Python
3.11; existing collector tests: 26 passing. Native integration uses actual wall
clock order, original mask, original FeatureJournal seeds, native Correct and
provenance, NativeDatasetSource/U05a, plus Undo. Explicit affected-set input in
that fixture is a caller-supplied test contract, not a claim of production
dependency inference. Missing/expired/partial/unverified evidence rejects data.

Full Windows discovery is being evaluated separately. The existing composed
benchmark fails its Windows temporary-SQLite cleanup before returning a result;
this is not accepted as a successful latency run. WSL/Linux host comparisons and
all four requested component cardinalities are being collected. Raw or initial
results are preserved where useful, with limitations stated explicitly.

## Continuation

1. Verify this branch/PR HEAD and full remote CI before declaring .1 complete.
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

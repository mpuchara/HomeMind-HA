# U05a: offline training data foundation on 0.14.232

Dependency: compatibility PR #355, rooted at production reference
`f7384ba5476f26155ecb138034cc3350fa5bfda2`. The integrated full local suite
passed before implementation started; subsequent Python 3.11/3.13 and image CI
also passed. See `UNIVERSAL_232_COMPATIBILITY.md` for exact evidence and limits.

## Implemented contract

`universal_training_data.py` supplies version-1 frozen contracts:

* `Supervision`: stable supervision/feedback identity, agent/generation/episode,
  selected decision time, label availability time, desired value, source,
  active/revoked state and weight.
* `HistoricalDecision`: detached immutable JSON receipt of the historical
  agent, DeviceAgent descriptor, legal mask, target state, required sensor set
  and target-affected sensor exclusions, with event/receipt/registry clocks.
* `ReplayCoverage`: a persisted collection/retention watermark proving a
  complete interval for the exact sensor set and source snapshot revision.
  Never fabricate this from row counts or current state. A retained seed alone
  cannot establish that no events were lost between seed and decision.
* `UniversalTrainingSample`: existing typed SensorTokens and dynamic ActionSet,
  desired action, registry revision, both contract versions, provenance, weight,
  generation/episode, label availability, deterministic sample id and source
  snapshot revision.
* `TrainingDataset`: deterministic ordered samples, explicit per-event rejection
  reasons, supervision revision and a content-derived dataset id.

No active model is changed and no model becomes trained by preparing data.
Scorer/encoder checkpoints remain untrained and observer-only.

## Native sources and causal gates

`read_supervision` reads the existing `ManualFeedbackJournal`, linked
`teach_rl_label` effects and `teaching_rl_labels.supervision_event_id`.
Explicit Correct, Teach and journal-qualified physical manual changes are
admitted; HomeMind actions, automations, Candidate/Shadow predictions,
physical outcomes and unknown sources are never positive action labels.
Unlinked legacy labels are not reinterpreted as trusted universal supervision.
The reader includes the requested root lineage so a revoked root event cannot
survive through an older active generation copy.

Undo/conflicts are resolved before deduplication. Repeated physical copies of
one event contribute once. Conflicting targets/desired actions reject the event.
No persistent consumed-id ledger is introduced: each new isolated model must
train from a fresh manifest. U05b must pin dataset/supervision revisions and
recheck current undo state before publishing. Undo of a previously trained
checkpoint requires an isolated rebuild, not an inverse weight update.

`prepare_dataset` requires a caller-owned consistent SQLite transaction. It
reads supervision, native FeatureJournal rows and the modern local archive
once in bounded passes. Both event and received time must precede each replay
decision. Archive rows with unknown receipt time cannot supply historical
context. Equivalent facts at identical clocks deduplicate with native journal
precedence; contradictory facts at identical clocks fail explicitly. Later
metadata is not merged into earlier receipts.

The builder reuses U02 `CausalObservationReplay`, `RegistryTimeline`, typed
normalization and U04 `from_device_agent_mask`. Historical registry information
and device authority must be available at the decision time. Missing receipts,
coverage, required sensor seeds or legal desired action make the sample
ineligible. Future sensor/registry changes never enter earlier inputs.
Later Correct information is permitted only as the label, and its created time
is retained for chronological training eligibility.

Known target-affected sensors are excluded. Power and illuminance inputs are
conservatively excluded until causal photometry is adapted. Unknown/unavailable
sensor masks remain explicit; entirely unusable context rejects the sample.

## Resource and execution boundaries

Defaults cap supervision at 2048 rows, the requested sensor set at 10000,
combined observations at 50000 (explicit maximum 100000), each source at
16 MiB of encoded payload, decision receipts at 16 MiB aggregate,
sample token references at 250000 and action references at 65536.
Per-receipt JSON is capped at 256 KiB. These are input/resource ceilings, not
measured peak RSS guarantees: Python object and SQLite overhead also exist.
Limits raise rather than returning an unnoticed truncated prefix.
Sample hashes are computed one sample at a time to avoid serializing an entire
large-home dataset into one temporary object tree.

Reads, merge and sample loops use the existing cooperative `TRAINING_BUDGET`.
The U02 journal reader adds optional byte bounds and a cooperative callback;
existing callers keep the same causal contract. SQLite connections/transactions
are owned and closed by the offline caller. No worker, HTTP route, model cache,
new dependency, options switch or automatic training job is installed.

```python
from universal_training_data import prepare_dataset, chronological_split

# receipts, registry_timeline and coverage come from persisted historical
# collection evidence for this read snapshot, never today's Engine/HA.
with store.conn() as conn:
    conn.execute("BEGIN")
    dataset = prepare_dataset(
        conn, root_agent_ids, receipts, registry_timeline,
        as_of_ts=dataset_cutoff, coverage=coverage,
    )
train, holdout = chronological_split(
    dataset, train_until=training_cutoff,
    holdout_from=future_start, holdout_until=future_end,
)
```

The split requires a positive temporal embargo. Labels created after the
training cut cannot enter past training. An episode appearing in both proposed
partitions is removed from both. There is no random row split or duplicated
supervision weighting across generations.

## Validation and remaining work

Tests exercise actual production journals, actual undo, root/child copies,
archive receipt eligibility, readonly consistent snapshots, future sensors and
topology, missing/gapped coverage, forbidden sources, legal desired values,
binary/continuous/categorical targets, deterministic rebuilds, resource limits
and independent future holdouts. Data cases cover 5/1, 50/10, 200/30 and
1000/200 sensors/devices. This is synthetic data-contract coverage, not a Pi4
timing or memory acceptance result.

Production observers currently keep their topology/masks only in RAM. This
change intentionally cannot make incomplete old installations trainable:
historical registry, action receipts and coverage collection still need a
durable opt-in producer before operational training is enabled. Existing
Correct broad snapshots are useful audit evidence but cannot replace these
strict receipts. Recorder history without original receipt evidence remains
ineligible. See `UNIVERSAL_U05_DATA_SOURCE_AUDIT.md` for source-by-source rules.

U05b is separate: freeze U03 initially, train a shared scorer with real gradient
updates and versioned checkpoints, recheck revoked supervision at publication,
and validate on this future holdout. U05c adds isolated local Correct adapters;
U05d uses the existing Candidate/Shadow lifecycle. None of those later stages
or physical-control eligibility is claimed by this foundation.

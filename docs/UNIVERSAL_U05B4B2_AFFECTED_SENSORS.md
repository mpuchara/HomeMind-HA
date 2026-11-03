# U05b.4b.2 — scoped affected-sensor evidence

Reference remains 0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
Dependency: Draft PR #361, `1b298554490a629cbc00d024c3879659bbfeb253`,
verified full CI [41613749739](https://github.com/mpuchara/HomeMind-HA/actions/runs/41613749739).
This stage does not enable collection/training/control, change Executor,
integrate Candidate, change Correct UI, refine retention, or publish artifacts.

## What the existing metadata proves

`device_explicit_mappings` describes device/property identity, autonomy, resource
groups and zones. HA registries describe membership and areas. Engine's
`dependency_agents` index routes policy inputs to agents. None is an exhaustive
physical affected-sensor contract. Shared rooms/devices, names, manufacturers,
and similar entity ids therefore never produce complete causal evidence here.

## Explicit operator API

`AffectedSensorDeclarations(store).declare(agent_id, *, logical_device_id,
topology_revision, affected_sensor_ids, complete, approved_by, rationale,
empty_reason=None, scope='collector_sensor_universe',
evidence_source='operator_declaration') -> revision` is an explicit local API
for an already authorized operator using an existing Store. It does not create
an HTTP endpoint or authenticate the caller; `approved_by` records attribution.
Call it outside WebSocket/Engine callbacks. No operator declaration is installed
automatically, and an installation need not enumerate every sensor to run.

```python
from universal_affected_sensors import AffectedSensorDeclarations

# Existing Store/Engine; caller has operator authorization and reviewed the scope.
agent = store.get_agent_config(agent_id)
descriptor = engine.executor.device_agents.descriptor(agent)
revision = AffectedSensorDeclarations(store).declare(
    agent_id,
    logical_device_id=descriptor['logical_device_id'],
    topology_revision=collector.topology,  # reviewed persisted historical topology
    affected_sensor_ids=['sensor.lamp_power', 'sensor.room_illuminance'],
    complete=True,
    approved_by='local operator reference',
    rationale='Operator reviewed all monitored sensors in this topology scope',
)
```

The API verifies target/sensor existence in that persisted topology, snapshots
native agent configuration and generation, and owns real approval/recording
clocks. It has no backdating parameter. The logical-device identity must also
match the ORIGINAL descriptor when the existing predecision worker consumes it.

The scope is the exact **enabled sensor/binary_sensor universe in the collector
topology**, bound by topology id plus a canonical universe hash/count. It is not
a claim about all unmonitored physical effects or unknown hardware. The compact
hash avoids repeating the entire home sensor list in every decision. Exact
membership is checked at declaration, preparation, collector write and U05a.

* `None`: unknown; never complete, never equivalent to `[]`.
* Nonempty list: known affected ids. Partial/complete are distinct declarations.
* `[]`: accepted only with `complete=True` and a nonempty `empty_reason`, after
  explicit review of this entire monitored scope. No inferred empty default.
* `automatic_hypothesis`: retained as `unconfirmed_hypothesis`, always incomplete.
  Future discovery can propose ids but must not attest completeness. Operator
  review creates a NEW operator declaration rather than rewriting the hypothesis.

The level `operator_attested_scoped` means an attributed operator assertion, not
mathematical or independently measured physical truth. Incorrect declarations
can exclude too few sensors. Completeness is trusted only at this stated level;
power/lux exclusions remain an independent safeguard.

## Storage, history and consistency

The API appends content-hashed rows in `universal_affected_declarations`, indexed
by `(agent_id, seq DESC)`. Row revisions include a nonce, exact target/property,
logical identity, generation/config hash, topology/universe binding, source,
trust, attribution, rationale and clocks. API updates never replace old rows.
Raw DB editing is unsupported; native revision triggers still detect such writes.

Newest assertion wins for future preparation. A partial/unknown replacement
withdraws future completeness without falling back to an older complete row.
It does not rewrite the fact that an earlier declaration was available to an
earlier decision. A future historical-correction/retraction policy, if required,
must be explicit; there is no retroactive physical-truth guarantee here.

The SAME PredecisionEvidence worker reads the latest declaration and verified
topology in its original native SQLite read snapshot. It checks config,
generation, device/property, membership, revision, checksum, scope and approval
clocks before publishing. `information_available_ts` is the worker's original
read-complete boundary after the committed declaration was visible, not an
invented earlier commit timestamp. Bind adds the original ActionIntent id/time,
agent/generation, registry revision and collector epoch using RAM only.

The declaration table joins the existing durable arbiter revision contract.
Any insert/update/delete before assertion persistence, including another process,
invalidates that prepared snapshot. State/config/registry/epoch matching and
lease/hold/dwell boundaries remain authoritative. Verification is deferred to
the collector writer transaction and can conservatively reject a change after
the decision but before writing. Prediction never waits for evidence or SQL.

## Collector and U05a

Engine's existing predecision argument now carries the affected contract.
Collector derives affected ids ONLY from that prepared contract; an arbitrary
`affected=()` cannot bypass a missing new-contract declaration. The writer checks
the exact latest native declaration, topology universe and original binding.

HistoricalDecision **v2** records both proofs, affected status and ids. Unknown
or invalid affected evidence remains an explicit incomplete v2 receipt, giving
U05a a specific rejection reason instead of silently making an empty set.
Legacy v1 explicitly supplied receipt API remains supported. Downgrading a
new-contract receipt to v1 while retaining its contract marker is rejected.

The existing prepare_dataset/native replay/lifecycle verify v2 completeness,
clock/identity/config/generation consistency, checksums and exact historical
universe against ReplayCoverage. A later declaration cannot qualify an earlier
receipt. Required history, legal desired action, provenance, deduplication and
Undo gates are unchanged. Affected sensors are excluded from inputs; power and
illuminance remain excluded even when not listed as affected. No new Correct
history, trainer, Candidate Manager or automatic promotion exists.

Limits reuse the bounded predecision cache/producer and dataset contracts.
Declaration body <=256 KiB; <=10,000 sensor ids; one indexed latest-row read,
not a replay of declaration history. Whole-home scope hashes are computed only
off the hot path. Generic target review limitations from .1 remain in force.
Retention remains conservative/global; granular witnesses belong to .3.

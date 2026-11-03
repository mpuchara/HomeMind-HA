# U05b.4b.3 — Granular FeatureJournal retention witnesses

## Contract and trust

V3 is newly collected coverage over an original, immutable native collection
segment. It is not an upgrade of V1/V2 evidence, a second observation journal, or
a claim that an operator assertion proves physical causality. FeatureJournal
remains the only native source of sensor values. Correct, Undo, provenance,
generation and original predecision/ActionIntent checks retain their owners.
Illuminance/power exclusions remain unchanged. Collection remains opt-in.

`ReplayCoverage.version == 3` adds:

* `topology_revision`, `historical_sensor_universe_revision/count`: the original
  enabled sensor universe from the recorded topology, independent of retention.
* `entity_ids`: the required history scope, derived from the union of original
  decision dependencies in the requested source. It does not redefine the
  universe asserted by an affected-sensor declaration.
* `retention_tracker`, `retention_origin_revision`: native tracker identity and
  damage revision captured before original seeds are written.
* `seed_event_keys`: original `(entity_id, event_key, event_time, received_time)`
  references. Old event clocks remain old; receipt clocks are never backdated.
* `granular_witness_json`: version, segment-proof checksum, topology/universe,
  required scope, start/through, original seed references, tracker, origin/issue
  revisions, completeness and canonical SHA-256 checksum.

Checksums detect inconsistent metadata; they do not authenticate an untrusted
database owner. As in earlier native contracts, application-owned native SQLite
schema and metadata are trusted. Missing/changed known trigger definitions fail
closed. Startup repair rotates the tracker identity and cannot restore old
authority. Arbitrary privileged rewriting of data, metadata AND trigger schema
between checks is outside this contract's threat model.

## Native atomic tracker

`universal_retention.py` owns `ensure_schema`, `origin`, `seal`, `make_witness`,
`verify_damage` and `verify`. The two metadata tables contain no observation
state or attributes:

* `universal_retention_meta`: singleton tracker UUID, revisions/floor, sealed
  receipt watermark, live row count, bounded-trigger bookkeeping.
* `universal_retention_entities`: at most 10,000 indexed entity damage envelopes
  `(entity_id, lo, hi, revision)` in **receipt time**.

Triggers on observation DELETE and meaningful UPDATE record damage in the SAME
SQLite transaction as the changed row. Both old and new identity/clock ranges
are damaged on UPDATE. Event key, contract, state, attributes, source, quality,
last-changed/updated and both clocks count as meaningful. Protection extension
alone does not. Backdated INSERT into already sealed receipt time records damage.
Normal later receipt INSERT does not invalidate earlier coverage.

The BEFORE INSERT guard also sees the old row before external `INSERT OR
REPLACE` can suppress DELETE triggers (`recursive_triggers` off). Live counts
remain correct with recursive triggers on or off. An exact duplicate native
UPSERT does not invalidate. A conflicting INSERT with changed values followed
by `DO NOTHING` can conservatively invalidate even though its conflict handler
kept the old row; the SQL statement transaction still rolls this back on failure.

The tracker remains installed after collection is disabled and is used by
separate ordinary SQLite connections/processes. ROLLBACK restores data and
damage together. A committed deletion survives process exit; an uncommitted one
does not. No RAM callback or best-effort post-commit notification grants trust.
Legacy retention epochs still advance conservatively.

## Bounded precision and retention work

One envelope per entity bounds space independently of event count, snapshots,
decisions and elapsed time. Damage in an unrelated entity cannot affect a
required entity. Single damage outside the attested interval does not overlap
it. Multiple disjoint mutations of the SAME entity merge to `[min, max]` and
can conservatively invalidate an interval between them. This is an explicit
precision budget, not proof that the interval actually lost an event. A unit
test preserves this adverse case. On entity-budget exhaustion the global floor
advances: older scopes reject instead of silently losing untracked damage.
Deleting/shrinking damage metadata also advances the floor.

Witness verification uses indexed entity lookups and original seed-key joins in
chunks of 128, not scans of sensor history. Witness JSON is capped at 2 MiB;
source, observation and collector queue caps remain in force. The existing
topology and native decision proof remain immutable and are independently checked.

FeatureJournal prune selects/deletes at most 512 candidate rows per batch for
age, per-entity and global caps. It preserves oldest-unprotected-first and then
protected-hard-cap priority. The collected database's native live count removes
the repeated full journal COUNT; a one-time migration COUNT initializes it.
Databases that never enabled collection keep the legacy COUNT path and have no
retention triggers. Batches remain in the original prune transaction/Store lock:
the candidate buffer is bounded, but a large prune can still occupy the writer
for the whole operation. This stage does not claim a hard writer-latency bound.

## Collection, replay and lifecycle

The collector captures tracker/revision before writing original snapshot seeds,
checks that no concurrent damage crossed that boundary, then persists the V3
native segment and seals its receipt watermark. Original WS barriers advance
the native segment and sealed watermark. Missing/incomplete snapshots remain
gaps. Registry/configuration/predecision/affected assertion handling is unchanged.

`source_from_collection(conn, agent_ids, start=..., through=...)` requires a
consistent transaction. It reads original decision dependencies, constructs and
verifies a scoped witness against the recorded segment, and exports the existing
`NativeDatasetSource` API. It checks accumulated damage even on FIRST export;
exporting after retention does not manufacture earlier completeness.

The witness covers the union of original dependencies of the requested agents
and interval. A joint multi-agent source rejects if any required part is lost;
an independent unaffected agent can be exported with a narrower `agent_ids`
request. This does not salvage an already trained joint artifact by silently
dropping its missing training history.

`prepare_dataset` verifies V3 against native metadata before replay. Only receipt
times in the attested interval are read; attested original seeds may have old
event clocks. Deleted seeds cannot be replaced by current HA state, enriched
archive rows, reinsertion under another key, or a later reconciliation. Required
loss rejects; unrelated sensor/seed loss preserves the same dataset identity.
Full historical topology/universe remains available to affected-sensor checks.
U05a checks membership against that full historical registry as well as the
native universe hash/count; matching scalar metadata alone does not establish
membership of a declared sensor or authorize a shrunken replay registry.

Checkpoint lifecycle snapshots use SQLite snapshot isolation. V3 publication and
guarded load recheck the scoped witness and current supervision revision INSIDE
the existing `BEGIN IMMEDIATE` acceptance transaction. A concurrent unrelated
SQLite COMMIT no longer revokes this source solely via global data_version.
Required loss and Correct Undo still reject before acceptance; guarded rank
retains its before/after freshness checks. No automatic trainer, Candidate,
Executor or neural Control activation is introduced.

V1 keeps its original trusted offline semantics. V2 keeps full-universe seeds
and global retention-epoch rejection. Installing the tracker advances that epoch
conservatively; no older segment acquires V3 trust. A V3 native segment cannot
be downgraded to V2 to bypass scoped checks.

## Validation and measurement

See `UNIVERSAL_U05B4B3_HANDOFF.md` for exact commits, executed tests, CI and
benchmark results. `tools/benchmark_universal_retention.py` exercises the same
original evidence fixture against either source root; it measures real native
prune, verification/dataset preparation, SQL trace counts, CPU, RSS, database
pages/object/index sizes, eligible samples and false invalidations. The composed
runtime driver retains baseline/current A/A and adverse measurements at all four
requested cardinalities. Host and synthetic fixture results are not Raspberry
Pi, real Recorder or physical HA acceptance.

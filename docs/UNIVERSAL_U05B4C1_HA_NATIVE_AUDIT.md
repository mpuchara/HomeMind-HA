# U05b.4c.1a — HA-native architecture audit

## Scope and version evidence

Baseline: exact Draft #364 `b3a67c0a2d264697ac69f5ee16bdd673b843f6d3`,
production reference 0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
This is the audit/profiling substage, not acceptance of U05b.4c.1b–d.

The user confirmed on 2026-11-04 that the installed HA Core version is
**2026.1.2** and selected it as the **minimum supported version** for this work.
This is the compatibility floor; the add-on manifest itself still has no Core
minimum declaration. Permissions, Recorder configuration and live API behavior
were not inspected. Reconnect/access and Recorder-load acceptance remain
**BLOCKER for HA-native adoption**, independently of the known version.

The primary compatibility source is Core 2026.1.2, commit
`ae17b620f69b7a132d32d2de1e30097f9a251616`. Inspection confirms entity selection
and include/exclude filters for `subscribe_entities`, event-type filtering for
`subscribe_events`, synchronous snapshot/listener setup, partial context deltas,
and explicit `significant_changes_only=0` for full REST history. Registry helpers,
history WebSocket and Recorder schema are included in the source manifest.
[Minimum-version commands](https://github.com/home-assistant/core/blob/ae17b620f69b7a132d32d2de1e30097f9a251616/homeassistant/components/websocket_api/commands.py)
and [history query](https://github.com/home-assistant/core/blob/ae17b620f69b7a132d32d2de1e30097f9a251616/homeassistant/components/history/__init__.py)
are official owning sources, not live integration tests.

Core 2026.10.4, commit `357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b`, remains an
additional comparison target, observed on 2026-11-03. Earlier 2025.1.4 inspection
is historical comparison only, not a supported minimum. Source inspection of
these versions does not certify every intervening release.
Official client state-delta reference:
`ad8031f63b996cccb9dc53633325c2039b030aee`. File URLs, hashes and sizes are in
`benchmarks/u05b4c1/ha-source-manifest.json`; upstream code is not vendored or
imported into the add-on. References below are public API documentation and
official owning source, not new dependencies on HA Python internals.

## Existing ownership and opportunities

| Responsibility | Current owning source | Decision |
|---|---|---|
| Device integrations and physical services | HA; `HAClient.service`, Executor | Retain HA ownership and independent state acknowledgement |
| Current state | HAEventStream, Engine RAM; REST fallback/resync | Retain RAM; evaluate compact transport separately |
| Registry metadata | HA registry lists/events; Engine/ContextEngine snapshots | Already HA-native; optimize repeated local work only with revision invalidation |
| Logical identity | DeviceAgentService: explicit mapping, HA device ID, exact entity fallback | No friendly-name inference; retain operator overrides |
| Standard imported history | HAClient.history, HistoryManager, local `entity_history` | Already Recorder-backed; investigate local duplication separately |
| Event/receipt-time evidence and protected windows | FeatureJournal and V3 retention | Preserve original clocks and native witnesses |
| Predecision mask, affected declarations, provenance | HomeMind | HA registries/context do not certify causality |
| Correct/Undo, Candidate/Shadow, learning/gates/artifacts | HomeMind | Remain independent edge AI responsibilities |

HomeMind already uses HA for integration, transport, registries, service
execution and historical retrieval. Its SQLite evidence/accountability work
cannot simply be delegated to HA. The profiled slow path reproduces without
an HA socket: transport is absent from that harness, while local locks/writes
remain. A transport change cannot by itself explain away those measured waits.

## WebSocket alternatives

The [public WebSocket API](https://developers.home-assistant.io/docs/api/websocket/)
provides event subscription and command results. Existing HAEventStream uses
one authenticated socket for `state_changed` and registry-update subscriptions,
then coalesced registry refreshes. Registry lists are not fetched per event.
REST is a fallback/reconciliation path; healthy operation uses a slower resync.

[Pinned command implementation](https://github.com/home-assistant/core/blob/357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b/homeassistant/components/websocket_api/commands.py)
confirms `subscribe_entities` supports entity selection and include/exclude
filters. `subscribe_events` filters event type, not an arbitrary entity list.
An empty `entity_ids` list means all entities. Permissions are checked when
forwarding; subscriptions are not a durable event journal or loss certificate.

| Variant | Experiment/result | Adoption decision |
|---|---|---|
| A: current original events | Frozen synthetic full-state/event corpus | Remains production transport |
| B: filtered compact state | Tested offline RAM cache, selected-state equivalence | Research only; no runtime wiring or evidence authority |
| C: logical dual subscription | Original bytes plus compact bytes; decoder costs reported as a sum | Not adopted: additive work and no end-to-end benefit established |

The [official delta encoder](https://github.com/home-assistant/core/blob/357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b/homeassistant/components/websocket_api/messages.py)
and [client cache logic](https://github.com/home-assistant/home-assistant-js-websocket/blob/ad8031f63b996cccb9dc53633325c2039b030aee/lib/states.ts)
inform the isolated prototype. Tests cover additions, removal, attribute
subtraction, partial context, original state clocks, unknown delta bases,
fresh snapshots after reconnect/subscription changes and stale sessions.
Unknown parent/user context is not invented from an ID-only initial state.
Malformed updates clear the cache and require resynchronization.

Compact changes omit the original event envelope, including its `time_fired`
and origin. A state context ID can span several related events; it is not a
unique event sequence number. No cross-subscription ordering or deduplication
contract is assumed. Both prototype `history_complete` and `evidence_eligible`
remain false. Transport arrival time must not be substituted for a missing
original local receipt time in historical replay.

**Open permission case:** revocation may suppress later updates without
removing a previously cached state immediately. An explicit fresh snapshot
clears the prototype, but automatic revocation detection on a real server is
unverified. Likewise, no live loss/coalescing/reconnect test was performed.
These limits block adopting B or C as an evidence source. If a later runtime
variant is justified, original collector events must retain their own verified
contract and must not run the same inference twice.

## Recorder and history

[Recorder documentation](https://www.home-assistant.io/integrations/recorder/)
describes configurable inclusion, exclusions, retention and multiple database
engines. There is no assumption that the household Recorder uses SQLite.
[History architecture](https://contributors.home-assistant.io/architecture/core/history/)
also describes deduplicated attributes and attributes deliberately not recorded.
The actual inclusion/retention rules and duplicated household row count are
unknown here, not zero.

| Question | Finding |
|---|---|
| All required entities/attributes retained? | Cannot infer from API presence; filters, unrecorded attributes and purge matter |
| State clocks/context stored? | Schema can retain them, but response modes may omit/reduce them; not original HomeMind receipt time |
| Reconnect gap repair? | Auxiliary history can help inspect a gap; it cannot certify that no transition was lost |
| Causal replay? | Needs the existing local clocks, topology, masks, declarations and durable V3 witness |
| Statistics? | Aggregated data cannot reproduce individual transitions or predecision facts |
| Availability/latency? | Real server/backend load was not measured; unavailable/minimal/missing responses remain unqualified |

The pinned [Recorder schema](https://github.com/home-assistant/core/blob/357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b/homeassistant/components/recorder/db_schema.py)
is inspected for stored clocks/context; direct schema reads are not added to
HomeMind. A database field existing does not guarantee it appears in a public
history response or was retained for this installation.

[Long-term statistics](https://www.home-assistant.io/docs/configuration/long-term-statistics/)
are summaries, not a substitute for exact event history. No hourly replay is
implemented. Public REST history remains auxiliary/offline and never enters
the event hot path. No direct Recorder DB access or writes were performed.

### Confirmed public-client bug and fix

`HistoryManager._fetch_history_resilient` requests `significant=False` for full
controllable-device imports, including brightness-only changes. Previously
`HAClient.history` omitted that parameter when false. Both
[2025.1.4](https://github.com/home-assistant/core/blob/2025.1.4/homeassistant/components/history/__init__.py)
and [2026.10.4](https://github.com/home-assistant/core/blob/357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b/homeassistant/components/history/__init__.py)
default omission to significant-only filtering. The failing contract test
demonstrates the missing query argument. The fix sends
`significant_changes_only=0` explicitly; the default true/minimal requests are
unchanged. Tests exercise the URL builder without contacting HA.

This can increase full-import rows/bytes, which is the requested correctness
contract, not a throughput optimization. Existing response caps, split requests
and background import scheduling remain. No old record, cached coverage,
timestamp or model was rewritten, and no backfill/training was started.
Previously imported windows may still lack transitions: this change does not
retroactively repair them or qualify historical AI samples.

## Decisions for the next substages

Keep the evidence journal and current transport. The offline native protocol
experiments are tools only and impose no normal runtime cost. Prioritize the
measured local write/lock work in 1b, then collector admission/throughput in 1c.
HA metadata can support structural indexes with versioned invalidation; shared
area/device membership must never approve affected-sensor declarations.
Any history reduction requires a separate equivalence proof for the exact
consumer. Preserve generic state/evidence boundaries so future non-HA adapters
can implement explicit contracts without importing HA internals.

## Implemented runtime/collector recovery follow-up (2026-11-04)

The audit's local-cost hypotheses led to implementation
`8036a2bd64dba32490d784df2029c0818ad3edbf`, stacked on Draft #365. No new HA
transport or Recorder adapter was adopted. The minimum Core remains the
user-confirmed **2026.1.2**. Native API adoption still requires its own live
permission/reconnect and historical-contract acceptance.

* Shadow schema readiness now uses an empty read of the required columns on
  each call. Ready schemas do not run DDL or acquire the global Store lock;
  missing optional columns still enter the original serialized migration.
  This is not the rejected research-only schema-once cache.
* Scheduler capacity deferrals are published together at the end of a pass,
  avoiding a lock acquisition for every waiting target. Routing, state/revision
  snapshots, worker capacity and target retry validation are retained.
* Collector idle admission tests unfinished futures, rather than an empty
  dictionary: completed futures remain indexed in the Engine. Under sustained
  foreground work, a bounded FIFO writer turn uses zero SQLite busy timeout,
  original receipt batches and native writer snapshots. SQLite atomically
  protects the lookup plus observation insert; producers do no SQLite.
* The original pruning operation remains pending if Store admission is busy;
  cutoffs, protected windows, V3 mutation triggers and disk-budget guards remain.
  A failed reusable-session open uses the existing native writer retry/reopen
  lifecycle. Partial snapshot metadata commits still require a conservative gap.

The improvements use the existing storage engine and ownership boundaries.
They do not reconstruct missing events from Recorder, invent local receipt
clocks, infer affected-sensor declarations or upgrade Executor-time masks into
predecision proof. See the [recovery report](UNIVERSAL_U05B4C1_RUNTIME_RECOVERY.md)
and [final handoff](UNIVERSAL_U05B4C1_FINAL_HANDOFF.md) for separate source identities,
controlled host results, remaining limits and CI evidence.

# Universal architecture compatibility with 0.14.232

Reference requested by the user: **0.14.232**, commit
`f7384ba5476f26155ecb138034cc3350fa5bfda2` (PR #348).
The actual GitHub main remains `91c7343324d38a1922bfed6486b4ef6dd7a3ba06`
(0.14.184); #348 and #349-354 were open drafts when inspected on 2026-11-02.
The requested reference is used without claiming an installed-device readback.
Version and installation artifacts are not bumped or repackaged.

## Audit and integration strategy

The comparison 0.14.184 -> 0.14.232 changes 548 paths; U01-U04c changes
27 paths. Six paths overlap: Engine, runtime composition, main shutdown,
settings, add-on configuration and CI. The machine-readable receipt,
including the production commit list, path statistics and exact protected
blob comparisons, is `benchmarks/UNIVERSAL_232_COMPATIBILITY.json`.

Integration starts at f7384ba and merges the exact U04c tip e2989925 into
a separate compatibility branch. This preserves both histories and every
newer production change; no force push, squash or merge into main is used.
The original six drafts are unchanged. Their old CI is evidence about
their old base only, not evidence of compatibility with 0.14.232.

| Stage | PR | Exact audited head | Original successful CI run |
| --- | --- | --- | --- |
| U01 | #349 | d32b8cf927fea368717624d3b9dcc3a2947e05d5 | 41248414460 |
| U02 | #350 | 89ea8e01acf10fc5466739a5b5f18fde8eb08148 | 41251493643 |
| U03 | #351 | deb5e9cdfe3398d6f0b756694c1f9a8024984fa9 | 41253441203 |
| U04a | #352 | a05c66a55a5dd0702f8c7cdbfc273a03be96d45e | 41254426030 |
| U04b | #353 | 1c54cb865bc5b85d77cb930bf24ad18699fe56ac | 41282317631 |
| U04c | #354 | e2989925573a5bc2748e87652570125232d3f69c | 41290556307 |

## Conflicts and protected contracts

* **Engine, real conflict:** U04b expected synchronous `flush_archive()` in
  REST reconciliation. Production 0.14.225 removed it so storage cannot
  block the sole poll worker. Keep the newer housekeeping persistence and
  live-card notification order; submit only the accepted merged state-map
  snapshot to the optional observer. Preserve websocket revision ordering,
  received timestamps, tombstones, sealed snapshots and scheduler behavior.
* **CI, real conflict:** retain the production scaling/Candidate/training/
  Correct and archive parity checks, then add both neural benchmarks.
* **RuntimeComposition, semantic overlap:** add lazy default-off installers
  after the existing Candidate neural Shadow installer. Keep startup scopes,
  candidate shadow persistence, final state/process pipeline assertions,
  explicit HTTP routes and existing installation order.
* **Main/settings/config, clean additive merge:** optional workers stop before
  existing durability/executor shutdown. Both observer switches remain false.
  Disabled configuration adds no observer thread, queue or neural inference.
* **Correct:** no overlapping neural edits. Keep the exact 0.14.232 data
  foundation, historical generations, margin/schema/classifier repair,
  manual journal, undo and UI. U05 must consume trusted supervision without
  reusing legacy broad snapshots as proof of universal causal completeness.
* **Candidate/Shadow:** preserve exact generation lineage, parent snapshots,
  sealed model ownership, deferred observation scheduling, bounded evidence,
  persistence, rollback and promotion gates. Neural observer projections do
  not enter promotion or active model training.
* **DeviceAgent/Executor:** exact 0.14.232 blobs retained. U04c consumes the
  current descriptor and legal mask read-only; resource ownership, leases,
  manual priority and final dispatch revalidation stay authoritative.
* **Training/storage:** exact admission, queue/process/budget, SQL syntax cache,
  causal observation and tournament queue implementations retained. No new
  trainer starts during compatibility integration.

Integration tests exercise the current DeviceAgent service, sibling ownership
blocking and enabled-observer REST refresh with archive persistence forbidden
on that thread. The existing suite covers Correct, Candidate, Shadow, HA
provenance and all intervening releases. Results are recorded below after
completion; passing a textual merge is not the acceptance gate.

## Acceptance boundaries

U05a implementation starts only after the integrated full suite passes.
Host/CI results cannot establish Raspberry Pi latency, websocket stability or
physical action/state ACK. Those target gates remain mandatory before release
or neural control. Existing ~300 ms p95 is user-supplied observational context,
not a measured outcome of this port. Keep both observers disabled by default.

## Validation receipts

Integrated code commit: `2ed401aac8a4f415017b64d1932f8aca61ed524a`.
Before U05a implementation, the complete WSL Python 3.12 / NumPy 2.3.3 suite
passed: **2903 tests in 453.717 seconds, OK**. The subsequently added two
production integration tests passed separately; all 81 universal tests passed.
Fresh [CI run 41391490830](https://github.com/mpuchara/HomeMind-HA/actions/runs/41391490830)
also passed Python 3.11 and 3.13 (full tests), image build and image smoke.
The package job was skipped; there is no install-release acceptance.

Short paired host runs use three Root agents, active Candidate, concurrent
training and Correct, two alternating repetitions, 60 events and eight warmup
events per run. All input/decision hashes agree. Event-to-decision p95:

| Routing | Baseline -> integrated | Same-source A/A control |
| --- | --- | --- |
| Shared sensor | 17.77 -> 17.11 ms | 17.10 -> 15.88 ms |
| Independent sensors | 15.04 -> 15.80 ms | 16.72 -> 20.93 ms |

The A/A noise is larger than the paired differences. These samples do not
prove a speedup or a precise no-regression bound, and are too short for p99
acceptance. CPU, peak RSS, samples, fixture flags and hashes are retained in
`benchmarks/UNIVERSAL_232_HOST_PARITY.json`. A physical Pi4 load matrix,
observer-enabled end-to-end profiling, actual HA connectivity/ACK and soak
remain open. U05a can proceed as offline data code with no runtime hooks.

# U05b.4b.2 handoff

## Branch and scope

Branch `codex/universal-u05b4b2-affected-sensors` starts at exact Draft PR #361
HEAD `1b298554490a629cbc00d024c3879659bbfeb253`. Its full CI
[41613749739](https://github.com/mpuchara/HomeMind-HA/actions/runs/41613749739)
was verified successful before changes. The initial worktree was clean.
Reference remains 0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
Keep Draft dependencies #355–361. No main merge, version/package, installation,
production enablement or automatic neural training/promotion is part of this work.

Implementation commit, PR and final CI results will be recorded in the validation
follow-up. Implemented: immutable scoped operator declarations, hypothesis/
partial/unknown/empty distinction, integration in the original predecision
snapshot/revision contract, exact ActionIntent binding, native collector receipts
and HistoricalDecision v2/U05a verification. Read
`UNIVERSAL_U05B4B2_AFFECTED_SENSORS.md` for the complete API and trust scope.

## Validation in progress

Focused new contract suite: 15 tests passing before final verification tightening;
latest exact-code focused result and complete CI will be recorded below. Existing
predecision 14 and collector 26 regressions also passed during implementation.
Native integration covers original received events, original masks/ActionIntent,
committed operator declarations, native FeatureJournal/Correct/provenance,
collector -> NativeDatasetSource -> U05a TrainingDataset and real Undo. Positive
tests do not override receipt timestamps. Negative corruption tests deliberately
alter exported fields/clocks and must reject them.

Initial host native component cases 5/1, 50/10, 200/30, 1000/200 obtained
1/10/30/200 complete assertions and 1/10/30/200 eligible fixture samples, with
zero dropped receipts or collector errors. Assertions describe a synthetic
fixture, not physical dependency truth in a real installation. Comparative A/A,
CPU/RSS/SQLite/write/size/prediction and runtime latency receipts are being
collected against exact PR #361. Initial/contended/unfavorable results are kept.

## Limits and continuation to .3

* No automatic causal inference from names, rooms, device membership or routing.
  Unknown/partial declarations remain ineligible; explicit empty requires an
  operator-scoped assertion and reason. Operator trust is not a physical proof.
* New operator declarations cannot repair old missing evidence. Stored historical
  assertions are not rewritten by later changes. Retracting an incorrect past
  physical assertion would need an explicit historical revocation policy.
* Generation/config/device/topology changes require a matching new declaration.
  Durable revision checks deliberately reject even unrelated covered writes,
  or changes after a decision but before persistence. Sustained activity may
  sacrifice evidence coverage; control is never blocked to improve yield.
* Generic number/select review limitations from .1 remain; power and illuminance
  training exclusions remain. No validated neural artifact or Pi4 acceptance is
  claimed here. Full validated-artifact integration belongs to .4.
* Begin .3 from this Draft HEAD after verifying its live CI. Keep FeatureJournal
  as the sole sensor history and ReplayCoverage/native witness checks intact.
  Implement sensor/window/topology-specific versioned retention witnesses with
  atomic invalidation in native deletion transactions, including restarts and
  disabled collection. Unverifiable older witnesses must fail closed.
* Retention must not consult current HA to refill missing history or reinterpret
  scoped operator assertions. Preserve both evidence versions, exact original
  decision binding, supervision provenance, deduplication and Undo.

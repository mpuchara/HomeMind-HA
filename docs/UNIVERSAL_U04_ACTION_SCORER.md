# Universal Neural Engine U04a — dynamic action interface and scorer

Status: **observer-only architecture step**, stacked on U03 (#351), U02
(#350), U01 (#349). No runtime integration, no model training, no release bump.

## Delivered

- adaptive_ai/src/universal_actions.py:
  - ActionCandidate and ActionSet: variable-number action descriptions keyed by
    exact target entity, logical device, physical property, context area,
    legal numeric action, option label, resource ownership and mask timestamp.
  - from_device_agent_mask takes a current agent, DeviceAgent descriptor,
    DeviceAgent.legal_action_mask result, HA target snapshot, and decision time.
    It rejects missing descriptors/masks, malformed rows, out-of-range values,
    unsupported unknown target state, disallowed autonomy, and missing
    resource ownership. Rows blocked by the existing arbiter are NOT scored.
  - This module does not invoke legal_value, implement its own reauthorization,
    or call the HA API. Its output is *not* an Executor authorization token.

- adaptive_ai/src/universal_action_scorer.py:
  - One shared float32 MLP on [home embedding, room embedding,
    device/domain/property/option semantics, action value/range/current state].
  - Domain/property/option labels use deterministic feature hashing; no fixed
    number of device IDs, option names or actions is baked into weight shapes.
    Hash collisions are possible. Training must measure their impact.
  - Accepts any number of proposals, evaluating in configurable batches
    <= 32, and preserving caller order.
  - Returns only raw, uncalibrated projections with confidence/reward/
    recommendation set to null; trained=false and control_eligible=false.
  - JSON-safe checkpoint rejects incompatible schema, NaN, invalid shapes
    and any attempt to load a trained=true checkpoint before U05.

- tests/test_universal_actions.py:
  - power, climate and select capabilities, including current options,
    legal mask denials, manual holds, resource guards, invalid data, 
    1000-option dynamic output and 200-device count stability.
  - checkpoint roundtrip, batching, scoring/memory constraints and
    compatibility with the U03 room/home embedding width.

- tools/benchmark_universal_actions.py:
  - synthetic all-device scan 2/20/100 targets x 2/16 actions, as CI
    informational job; not an actual Pi4 event-to-decision performance gate.

## Boundaries / required steps before control

The scorer is NOT trained and is NOT installed in the running add-on.
Random projections are not a policy. Never derive a decision, confidence,
reward, promotion eligibility, or ActionIntent from this result.

ActionCandidate reflects a **caller-supplied mask**, not an immutable
authorization. The trusted DeviceAgent/Executor must independently enforce
current control eligibility, explicit generic-target approval,
capability fingerprint, manual priority, leases, dwell, cooldown,
safety limits, and physical command composition AT DISPATCH TIME.
An earlier mask or an older state snapshot cannot be used as approval.

**No implicit addition of new autonomous device domains:** unknown or
undescribed domains require the existing approved adapter and operator
description. The descriptor and action mask must be consistent and reflect
the appropriate live vs historical trust boundary.

The U03 representation currently captures room sensors plus a shared home
context but omits many variables needed for a deployable world model:
causal photometry, temporal dynamics, detailed room geometry, physical
process evolution, user feedback, policy calibration, long-term comfort,
context quality, and confidence. The U04a action model is only the skeleton.
The U03/U04 fixed embedding sizes are hypotheses, not tuned results.

## Follow-up: Codex-suited U04b/U05

1. Implement an explicitly disabled-by-default **observer-only runtime
   adapter** with one serialized HA event consumer, bounded queue,
   dependency routing and deadlines. Never enqueue full neural scans
   on the HA ingress thread.
2. Align U02 historical context across FeatureJournal + Recorder/Archive
   with provenance, event_time/received_time, old unit versions and
   historical registry epochs. No hindsight from current HA metadata.
3. Implement deterministic candidate generation using present agent
   action_values + legal_action_mask as an input, with capability
   fingerprint checks and full audit of why actions were disallowed.
   A blocked choice must never surface as an action candidate.
4. Build a supervised **trainable** shared action-conditioned scorer
   with frozen U03 room encoder initially, bounded replay and inference
   budget. Keep distinct manually requested changes, Correct labels,
   executed actions, auto events, actual outcomes, and trusted rewards.
5. Validate action ranking and per-agent calibration on future holdouts,
   including devices/rooms not seen during training; keep neutral
   baselines and strict no-regression checks for all impacted agents.
6. Preserve Candidate generation/parent lineage, multi-point Correct,
   Shadow decisions and promotion/rollback. Compare paired events to the
   existing Ridge/TinyMLP model. Enable control only after explicit safety
   and Pi4 event-to-decision p95/CPU/RSS release gates.
7. License the repository explicitly and audit dependencies before
   publishing as open source; a public GitHub repository is not enough.

## Acceptance and review

    python -m unittest discover -s tests -p 'test_universal_actions.py' -v
    python tools/benchmark_universal_actions.py --devices 2,20,100 --actions 2,16 --passes 5

Full GitHub CI: Python 3.11 and 3.13 plus Docker image smoke. Synthetic
timings are not production metrics. Merge U01 -> U02 -> U03 -> U04a, without
enabling any new Control path.

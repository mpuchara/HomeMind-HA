# Candidate Recovery — component checkpoint

Release gate: **NOT RUN**. Four new component regressions passed on WSL
Python 3.12.3 against runtime `f858414c`; they do not certify production recovery.
No queue limit, drop policy, timeout or training/learning behavior was changed.

`tests/test_final_release_candidate_recovery.py` executes the real
`DeferredCandidateShadowQueue` with a controlled executor:

| Case | Status | Verified outcome |
| --- | --- | --- |
| 40 roots into a bounded 4-root test queue; drain and new wave | PASS | Exactly 36 explicit evictions, retained roots drain, all four fresh roots process the new causal revision |
| Generation changes while jobs await service | PASS | Four obsolete jobs are rejected; the fresh generation drains without restoring old work |
| One executor error followed by healthy roots | PASS | Error counter and diagnostic event expose failure; peers proceed and the failed root can process a later job |
| A queued peer changes while the preceding root runs | PASS | Drain takes that peer's newest exact causal job rather than a detached stale batch |

The small queue is a deliberate component fault fixture, not a reduced acceptance
benchmark. Its backend is controlled, not a real model trainer. The overload
contract explicitly drops/coalesces work; PASS is not a lossless-learning claim.
The first error test expected `last_error` to remain set after successful peers.
The queue intentionally clears that field after success; the final test verifies
the cumulative error counter and emitted diagnostic event instead. The initial
failure and corrected run are retained in the evidence archive.

Still required before the gate can PASS:

- Real multiple-agent Candidate/Shadow inference overlapping training and evidence preparation.
- Overload/drain/fresh-wave comparison with preparation enabled and disabled.
- Actual training interruption/restart/resume, SQLite/storage faults and durable queue outcomes.
- Discard/new-generation lifecycle through the complete installed wrappers.
- Controlled WebSocket loss/recovery, real final HTTP routes and refreshed Current/Desired.

The source audit confirms TrainingQueue is intentionally in RAM. Candidate's
durable lifecycle can requeue interrupted builds; that is distinct from resuming
every pending in-memory training request or preserving every discarded observation.
These outcomes need their own measured acceptance results.

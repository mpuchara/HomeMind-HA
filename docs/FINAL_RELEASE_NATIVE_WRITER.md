# Native writer investigation — 2026-11-06

Gate D remains **BLOCKER**. No release, version, cadence, qualification or
throughput requirement changes are authorized by these diagnostics.

Resume checked at local and fetched origin HEAD `79bf067432c2cf6b7e0c4122352a6c609bb36067`;
worktree was clean and `git diff eb6650e2 HEAD -- adaptive_ai` was empty.

## Observation before implementation

Disposable WSL ext4 root: `/home/mpuchara/homemind-writer-20261106`.
`compatible-profiles/small50-current.json` uses the current identical-compatible
benchmark, 24 ticks, 400 ms period, fanout 16, 50 sensors/10 roots, native persistence.
The diagnostic-only AST probe times bounded operations using thread CPU and
records aggregate call-site counts. It records transaction context entry/exit,
journal preparation/protection/insertion, revision and binding validation,
affected declaration/topology, receipt construction/serialization/persistence.
It retains no event payloads. Instrumentation overhead is included; this is
not latency or end-to-end performance acceptance.

All **26** observed journal protection reads found an empty
`feature_window_entities` table in the authoritative writer transaction.
The original grouped protection queries consumed **9.605 ms CPU** in total.
Repeated SQL text construction was below 0.1 ms in an earlier diagnostic;
caching that text is not justified as the primary writer improvement.

Proposed first removal: when a fresh SELECT in the SAME BEGIN IMMEDIATE
transaction proves there are no window/entity links, every original protection
join is empty and its result is exactly zero. Use those zeros directly. Keep
the complete original grouped query for every nonempty table, regardless of
whether its rows appear relevant. Do not retain the emptiness result beyond
this invocation. External writers cannot add links while BEGIN IMMEDIATE owns
the writer reservation. No native write intervenes between the empty check and
the original observation insertion. Insertion triggers remain visible to the
next batch. The oracle computes protection before those triggers as well.

The frozen pre-change method is `tests/reference/journal_record_batch_79bf0674.py`.
Compare exact keys and every journal/window/link column, including floating
protection values, duplicates, late samples, overlapping/disjoint windows,
multiple agents/shared sources, insertion-trigger changes and rollback.
No revision, schema, causal binding or receipt verification is reused/skipped.

Earlier probes are preserved, not acceptance: `initial.*` includes preparation
calls and per-item instrumentation; `rejected-intrusive/` has excessive per-item
overhead; `harness-mismatch-profiles/` has differing harness hashes and includes
an interrupted arm. The corrected probe excludes per-item calls, scopes to
writer execution and requires a common harness placed inside each disposable
source tree. Never include those rejected measurements in the acceptance matrix.

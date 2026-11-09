# Gate D decision

**Feasibility verdict: CURRENT REQUIREMENTS NOT DEMONSTRABLY ACHIEVABLE.**

**Recommended next action: Option 3 — GATE D CRITERIA REVIEW REQUIRED.** Open a review of whether the current synthetic tail and process-CPU margins reliably measure the product risk they are intended to protect. Use the bounded diagnostic below as the evidence-gathering step for the review. Gate D remains BLOCKER under the original criteria. The review proposal requires an explicit product decision before any criterion could change.

## Why this direction

- **MEASURED.** The retained architecture clears all four 2× aggregate evidence targets in the registered matrix, but simultaneous timing acceptance is 9/13. The biggest gap is 50/10 CPU, with ~91 ms more savings needed in the failed run. Same-revision variance can cross the CPU and tail margins. Both findings must be kept in the review.
- **MEASURED.** Current admission can lose an idle window, and one recheck recovers the missed evidence in controlled replay. Its two unprofiled 50/10 failures preclude retaining that candidate. New traces place the p99 boundary in first-wave dispatch with no overlapping preparation. The cause of the original two latency failures is **UNKNOWN**.
- **INFERRED.** The strongest measured levers are the number of native writer turns and the timing of preparation access. No remaining implementation has a quantified, repeatable gain large enough to justify another optimization. A review costs less and has less safety risk than adding a worker before assigning the residual CPU and wait.
- **UNKNOWN.** The product value of the extra historical qualification and actual Pi/HA latency/CPU trade-off remain unmeasured.

## Options considered

| Variant | Assessment and risk |
| --- | --- |
| A. Current worker, scheduler and transactions | **MEASURED.** Registered throughput has already passed with these constraints. The verified storage and receipt lexical redundancies yielded single-site savings, with mixed whole-process effects. **UNKNOWN** further safe yield. The largest plausible remaining lever is repeated guarded transactions, but different batching would need explicit boundaries, durability and causal equivalence. |
| B. Prepare on a separate worker | **INFERRED.** May let preparation use an eligible interval while the writer is busy. It cannot create eligibility while original inference is active or exceed an unchanged preparation budget. It adds queue handoff, GIL/CPU and SQLite contention, plus fresh RAM and source-revision validation at consumption and native commit. Fail-closed handoff must bind original agent, generation, model, epoch, state, causal declaration and original clocks. Pi trade-off is **UNKNOWN**. |
| C. Prepare immutable structure earlier | **INFERRED.** Versioned observation schema, static topology indexes and canonical encodings of immutable declarations are potential reusable inputs only under exact identity/invalidation rules. The current native snapshot already reuses validated structure. The original legal mask, live state and holds, original decision clocks and generation/model/epoch binding still require fresh verification. Triggers, DDL and rollback can invalidate native assumptions. Any final write retains authoritative native checks. Previous derived-universe reuse did not clear the whole-system gate. |
| D. Review product requirements | **INFERRED.** Keep safety, identity, retention and native persistence as independent requirements. Review separate absolute foreground latency and CPU/resource budgets for a named Pi/HA composition, with evidence readiness/coverage and fail-closed accounting as separate targets. Keep the registered synthetic run as a comparative screen during review. A production limit could miss scale regressions if chosen from narrow data, and a weaker evidence target could reduce learning coverage. Establish product budgets before approving changes, with populated data, candidate work, SQLite contention, restart and a soak. No numeric replacement is approved or proposed from WSL. |

**INFERRED cost and benefit.** Plan roughly one engineer-day for the bounded measurement and review preparation once suitable tracing is available. A worker split is a multi-day design and validation effort before representative soak and Pi time, with a much larger correctness surface. Reconsider it only if repeated traces assign a material share of missed eligible windows to writer occupancy and the same measurement can quantify a safe improvement. A split would need opt-in/default-off rollout, compatibility with existing native data, stop/restart/rollback of both workers, original receipt equivalence, contention and retry tests, and original Gate D comparisons. No design work was started.

## Bounded experiment for the review

Use only 50/10, unchanged fixture/workers and original cadence, with the exact #368, retained and previously rejected sources. Pre-register eight serialized, balanced blocks containing an unprofiled #368/retained pair, an unprofiled retained/retained control and a separate lightweight retained/rejected trace pair. Preserve one full stream per arm and retain every result. Capture exact producer receive/intent samples joined to tick/target, monotonic enqueue/dispatch/inference, Engine and Context lock, native SQL/transaction and writer/prepare spans. On a trace-capable host record thread run-queue time, host runnable pressure and WAL I/O. Bound probe overhead with matching unprofiled controls. Use the existing replay only as an oracle for the missed window. At historical ~13 s per arm the timed stream suite is roughly 10–11 minutes of machine time before trace setup, validation and analysis.

**Decision rule.** If the retained/#368 delta consistently exceeds the control spread and repeated tail decisions join to the same measured wait or CPU stage, quantify that cost and revisit a targeted design. If control spread overlaps the proposed effect or traces attribute tails to host/first-wave timing, bring those results to the criteria review alongside a representative Pi/HA measurement plan. If exact lock/CPU attribution remains unavailable, record the limit and avoid a causal claim. The experiment informs the review and leaves registered acceptance intact.

## CI audit

**PROVEN.** [Run 42674180258](https://github.com/mpuchara/HomeMind-HA/actions/runs/42674180258) is FAILURE on exact `dba0ceb1`. Actions Jobs and Checks list both Python matrix jobs as successful, `package` as skipped and no `image` job. The workflow summary has an error annotation from GitHub, “Internal server error”, correlation ID `adf46a00-1ff8-463f-8029-9e1cd1ed7258`. The same workflow on [42666974689](https://github.com/mpuchara/HomeMind-HA/actions/runs/42666974689) succeeded with image and with package skipped. The workflow diff between those commits is empty. The image job has no `if` or `needs`. The package job depends on tests and is restricted to pushes to main. The package skip is expected.

**INFERRED.** The server-side orchestration failure is consistent with the missing image job. The exact internal cause is **UNKNOWN**. A separate same-SHA failed attempt, [42674168788](https://github.com/mpuchara/HomeMind-HA/actions/runs/42674168788), has a successful image check and lacks the 3.11 job. That partial success does not turn either workflow green. Verify the complete required workflow for the new reporting head. CI requirements are unchanged.

## Release implications

**MEASURED.** Gate D timing remains a blocker under the registered criteria. Candidate Recovery, production composition, migration using actual installation data, intermittent retention/rollback, four-hour soak, release integration and live Pi/HA acceptance remain open. Draft PR #369 stays open, the retained runtime tree and version 0.14.232 are unchanged, and the Collector remains default-off.

[Feasibility and product assessment](FINAL_RELEASE_GATE_D_FEASIBILITY.md) · [Cost and tail evidence](FINAL_RELEASE_PERFORMANCE_LIMITS.md)

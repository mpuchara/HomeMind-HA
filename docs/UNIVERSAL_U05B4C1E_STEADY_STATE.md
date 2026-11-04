# U05b.4c.1e steady-state experiment

## Protocol registered before comparison runs

Runtime sources are #364 `b3a67c0a2d264697ac69f5ee16bdd673b843f6d3`,
#365 `613e2e2638df6ac58b873bbcc2a93d78fd63a8c2` (diagnostic),
and #366 `7cf56533187a7db43a0d5d384b8e82444c0108d1`. One common harness
drives all three. All runs are WSL host-only, serial, fresh processes,
8 warmup and 40 steady steps, with an original 30 s observation deadline.
The collector, Candidate, normal Shadow inference, isolated background
training and Correct/Undo are active.

Matched mode releases a one-slot batch at every event, with 64 isolated
policy updates and two model writes at offsets 0 and 32. No next event is
released until both the observation and the training batch finish.
The run records relative monotonic release, trainer start/end and
observation times. This controls arrival phase and counts. Actual overlap,
the Candidate queue and contention remain outcomes. Natural mode uses
the continuous background fit and model writes without the batch barrier.
Correct runs at steps 0 and 25 and all root agents have a Candidate.

The primary 1000/200 series has four matched pairs and two separate
natural pairs. Order alternates baseline/current and current/baseline
within each series. Each smaller scale has a matched and a natural pair.
If the four large matched pairs are inconclusive, add pairs 5 and 6,
preserving earlier attempts. The separate diagnostic series includes #365
at 200/30 and 1000/200 and enables the existing probes. Probe results
are analyzed separately from primary runs.

For functional parity require equal input and decision hashes, equal
observed intent counts, zero HA calls and matching Correct/Undo results.
Check 30 s warmup separately. Collector durability requires physical
row parity and loss-gap checks from its own diagnostic benchmark.
Incomplete pairs are retained and cannot satisfy non-inferiority.

Experimental non-inferiority margins are +10% p95 and +15% p99.
Use paired run ratios and report each independent pair. To call
`NO_REGRESSION`, require at least four complete large matched pairs and
a one-sided exact paired sign test at 0.05 against each margin. With four
pairs this cannot pass, so extend to six if an inference is still needed.
Require natural-workload corroboration with both large pairs complete
and within the same margins. Any other evidence is `INCONCLUSIVE`
unless independent runs support a repeated regression. Report p50, max,
CPU and RSS separately. These margins are experimental. Multi-second
tails remain a separate `SCALABILITY_TARGET_NOT_MET` concern.

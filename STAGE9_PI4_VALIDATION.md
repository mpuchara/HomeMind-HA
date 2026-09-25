# Stage 9 — Raspberry Pi 4 validation and controlled-rollout evidence

Release target: **0.14.88**

This stage deliberately separates measurement from authority. The release can collect and
evaluate real Raspberry Pi 4 performance evidence, but it does **not** unlock Stage-7
Offline-RL Candidates for Control. A green profile suite is a prerequisite for a later,
explicit controlled-rollout change.

## Required scenarios

Run all scenarios on the same installed release and the same Raspberry Pi 4:

1. `idle` — normal runtime, no explicit training.
2. `training` — one agent actively training.
3. `correct` — use the normal multi-point Correct UI while collecting; pass the current
   Correct history GET as `--correct-path`.
4. `training-correct` — one training worker plus normal Correct interaction.

Generate real HA state changes during every scenario so `telemetry.event_to_intent` is
present. The profiler never generates service calls or synthetic HA traffic.

Recommended duration: 120 s for idle/correct and 180 s for training/training-correct.
The hard gate requires at least 60 s.

## Collector

Inside the add-on container:

```sh
python /app/pi_training_profile.py \
  --scenario idle --duration 120 \
  --output /data/pi4-idle.json

python /app/pi_training_profile.py \
  --scenario training --duration 180 \
  --output /data/pi4-training.json

python /app/pi_training_profile.py \
  --scenario correct --duration 120 \
  --correct-path "/api/agents/<AGENT_ID>/teach-rl-history?start=<UNIX_TS>&end=<UNIX_TS>" \
  --output /data/pi4-correct.json

python /app/pi_training_profile.py \
  --scenario training-correct --duration 180 \
  --correct-path "/api/agents/<AGENT_ID>/teach-rl-history?start=<UNIX_TS>&end=<UNIX_TS>" \
  --output /data/pi4-training-correct.json
```

The Correct path must be a read-only `/api/...` GET. Use a range that actually contains
the decision history shown by the normal Correct chart.

## Gate

```sh
python /app/pi4_release_gate.py \
  --idle /data/pi4-idle.json \
  --training /data/pi4-training.json \
  --correct /data/pi4-correct.json \
  --training-correct /data/pi4-training-correct.json \
  --output /data/pi4-stage9-gate.json
```

Exit codes:
- 0 — pass;
- 2 — fail;
- 3 — inconclusive / missing required evidence.

## Default hard gates

- all reports identify a real Raspberry Pi 4 and the same release;
- local `/api/status` p95 <= 500 ms and p99 <= 1000 ms;
- Correct history p95 <= 500 ms and p99 <= 1000 ms;
- `event_to_intent` p95 <= 500 ms;
- training and training+Correct `event_to_intent` p95 <= 2x idle;
- <= 1% local status probe failures and no more than one consecutive failure;
- <= 1% HA/realtime disconnect samples and no more than one consecutive disconnected sample;
- never more than one training worker;
- Adaptive AI runtime+worker average CPU <= 50% of the Pi's total CPU capacity;
- whole-system CPU p95 <= 90%;
- combined runtime+worker p95 RSS <= 768 MB;
- at least 256 MB MemAvailable;
- temperature <= 80 C when the thermal sensor is exposed to the container.

CPU semantics are explicit: `cpu_one_core_percent=100` means one fully occupied core;
`cpu_host_percent` divides Adaptive AI CPU by the logical CPU count. Separately,
`host_runtime.system_cpu_percent_*` is sampled from `/proc/stat` and represents the whole Pi,
including Home Assistant and other processes visible to the add-on container.

## Interpretation

A green result says the measured release met the Stage-9 runtime gates on the measured Pi.
It is **not** permission for Offline-RL physical authority. In 0.14.88 the Stage-7 promotion
block remains unchanged. The next development step consumes the real profile suite,
tunes any failing path, and only then can an explicit canary-Control design be considered.

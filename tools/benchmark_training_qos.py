#!/usr/bin/env python3
"""Deterministic virtual-time QoS benchmark for cooperative historical training.

This benchmark does not claim Linux process CPU utilisation.  It measures the scheduler's
own wall-clock work/sleep accounting and proves that realtime priority cost is scoped to
bursts rather than multiplied by replay checkpoint count.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from training_budget import CooperativeTrainingBudget


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)


def budget(clock):
    item = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
    item.configure(
        duty_cycle=.65,
        max_slice_seconds=.035,
        max_sleep_seconds=.5,
        thread_prefixes=("worker",),
        realtime_max_burst_seconds=.45,
        realtime_cooldown_seconds=.20,
    )
    item.begin(thread_name="worker")
    return item


def micro_checkpoint_burst(count=64):
    clock = VirtualClock()
    item = budget(clock)
    item.request_interactive_window(.30, reason="ha_state_changed")
    useful = 0.0
    sleeps = 0.0
    for _ in range(int(count)):
        clock.now += .0001
        useful += .0001
        sleeps += item.checkpoint("micro", thread_name="worker")
    snap = item.snapshot()
    if snap["interactive_preemptions"] != 1:
        raise AssertionError(snap)
    if float(snap["interactive_sleep_seconds"]) > .036:
        raise AssertionError(snap)
    return {
        "checkpoints": int(count),
        "useful_work_seconds": useful,
        "returned_sleep_seconds": sleeps,
        "virtual_elapsed_seconds": clock.now,
        "stats": snap,
    }


def steady_traffic(hz, seconds=4.0):
    clock = VirtualClock()
    item = budget(clock)
    next_event = 0.0
    events = 0
    useful = 0.0
    work_quantum = .005
    while clock.now < float(seconds):
        while clock.now + 1e-12 >= next_event:
            item.request_interactive_window(.30, reason="ha_state_changed")
            events += 1
            next_event += 1.0 / float(hz)
        clock.now += work_quantum
        useful += work_quantum
        item.checkpoint("steady", thread_name="worker")
    snap = item.snapshot()
    if useful < .50:
        raise AssertionError({"hz": hz, "useful": useful, "stats": snap})
    if float(snap["interactive_sleep_seconds"]) >= float(seconds) * .5:
        raise AssertionError({"hz": hz, "stats": snap})
    if int(snap["slice_checkpoints"]) <= 0:
        raise AssertionError({"hz": hz, "stats": snap})
    return {
        "hz": float(hz),
        "events": events,
        "useful_work_seconds": useful,
        "virtual_elapsed_seconds": clock.now,
        "stats": snap,
    }


def user_escalation():
    clock = VirtualClock()
    item = budget(clock)
    item.request_interactive_window(.30, reason="ha_state_changed")
    first = item.checkpoint("realtime", thread_name="worker")
    item.request_interactive_window(.90, reason="correct_label")
    second = item.checkpoint("correct", thread_name="worker")
    snap = item.snapshot()
    if snap["interactive_priority_epochs"] != 2:
        raise AssertionError(snap)
    if snap["interactive_epoch_escalations"] != 1:
        raise AssertionError(snap)
    if first <= 0 or second <= 0:
        raise AssertionError({"first": first, "second": second, "stats": snap})
    return {"first_yield": first, "second_yield": second, "stats": snap}


def run():
    traffic = [steady_traffic(hz) for hz in (2, 4, 10)]
    return {
        "contract": "training_qos_burst_epoch_v1",
        "semantics": "cooperative_wall_clock_not_process_cpu",
        "micro_checkpoint_burst": micro_checkpoint_burst(),
        "steady_traffic": traffic,
        "user_escalation": user_escalation(),
        "pass": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(), ensure_ascii=False, indent=None if args.compact else 2))


if __name__ == "__main__":
    main()

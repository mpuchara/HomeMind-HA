"""Short coalescing window for expensive Candidate rebuilds.

Teach points are normally entered in a burst. Starting a one-to-two-hour historical rebuild
after the first click would guarantee another stale rebuild a few seconds later. Keep the
Candidate visibly queued, collect rapid Wrong decision/Teach revisions for 15 seconds,
then start one build. Explicit "Train RL" and manual Rebuild requests bypass the delay.
"""
import time


DEBOUNCE_SECONDS = 15.0
IMMEDIATE_REASONS = {"teach_train", "manual_rebuild"}


def install(manager):
    if getattr(manager, "_candidate_debounce_installed", False):
        return manager
    original = manager._start_build

    def start_build(row):
        reason = str(row.get("reason") or "")
        queued_ts = float(row.get("queued_ts") or 0.0)
        if reason not in IMMEDIATE_REASONS and queued_ts > 0:
            remaining = DEBOUNCE_SECONDS - (time.time() - queued_ts)
            if remaining > 0:
                return False
        return original(row)

    manager._start_build = start_build
    manager._candidate_debounce_installed = True
    manager.candidate_feedback_debounce_seconds = DEBOUNCE_SECONDS
    return manager

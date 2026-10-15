"""Shorter cooperative Python timeslices for realtime I/O among CPU workers.

This is not a deadline, OS priority change, or a cap on native SQLite I/O.
Apply only in the realtime entrypoint, never in isolated training processes.
"""
import sys

_requested_ms = None


def configure_runtime_threads(options):
    global _requested_ms
    try:
        milliseconds = max(0, min(10, int(options.get('runtime_thread_switch_ms', 1))))
    except (TypeError, ValueError):
        milliseconds = 1
    _requested_ms = milliseconds
    if milliseconds:
        sys.setswitchinterval(milliseconds / 1000)
    return runtime_threading_snapshot()


def runtime_threading_snapshot():
    return dict(configured_ms=_requested_ms, actual_ms=sys.getswitchinterval() * 1000,
                implementation=sys.implementation.name,
                semantics='cooperative_interpreter_timeslice_not_a_deadline')

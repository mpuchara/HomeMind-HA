"""Monotonic HA action timing. Shadow never contributes service/ack samples."""
import time
from telemetry import TELEMETRY


def service_started(engine, runtime, started_ts):
    received = getattr(getattr(engine, '_inference_tls', None), 'event_received_perf', {}) or {}
    values = [float(x) for x in received.values() if x is not None]
    started = time.perf_counter()
    runtime.update(event_to_service_ms=None, event_to_ack_ms=None, service_to_ack_ms=None,
                   ack_latency_seconds=None)
    runtime['_service_timing'] = dict(started_perf=started, started_ts=started_ts,
                                      received_perf=min(values) if values else None)
    if values:
        runtime['event_to_service_ms'] = max(0., (started - min(values)) * 1000)
        TELEMETRY.observe('event_to_service_dispatch', max(0., (started - min(values)) * 1000))
    return started


def service_returned(runtime, started, success):
    now = time.perf_counter()
    TELEMETRY.observe('ha_service_request', max(0., (now - started) * 1000))
    timing = runtime.get('_service_timing') or {}
    received = timing.get('received_perf')
    if success and received is not None and timing.get('started_perf') == started:
        TELEMETRY.observe('event_to_service_return', max(0., (now - received) * 1000))
    if not success:
        runtime.pop('_service_timing', None)


def acknowledged(runtime, pending):
    timing = runtime.pop('_service_timing', None) or {}
    if pending.get('no_service') or timing.get('started_ts') != pending.get('started_ts'):
        return
    now = time.perf_counter()
    if timing.get('started_perf') is not None:
        runtime['service_to_ack_ms'] = max(0., (now - timing['started_perf']) * 1000)
        TELEMETRY.observe('service_to_state_ack', max(0., (now - timing['started_perf']) * 1000))
    if timing.get('received_perf') is not None:
        runtime['event_to_ack_ms'] = max(0., (now - timing['received_perf']) * 1000)
        TELEMETRY.observe('event_to_state_ack', max(0., (now - timing['received_perf']) * 1000))

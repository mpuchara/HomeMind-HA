"""Bounded RAM staging for Recorder import, independent of model-worker memory."""
from pathlib import Path


def preparation_profile(options, memory, rss_mb=None):
    """Use measured headroom; unavailable memory information keeps the old 500 MiB cap."""
    ceiling = max(256, min(2048, int(options.get('training_preparation_memory_limit_mb', 1536))))
    total, available = memory.get('total_mb'), memory.get('available_mb')
    if (not isinstance(total, (int, float)) or not isinstance(available, (int, float))
            or total <= 0 or available <= 0):
        limit = min(ceiling, 500)
        headroom = 0
    else:
        current = max(0., float(rss_mb or 0.))
        headroom = max(0., available - 512.)
        limit = int(min(ceiling, max(256., total * .20),
                        max(256., current + headroom * .30)))
    rows = max(128, min(1024, int(options.get('training_recorder_batch_rows', 1024))))
    if limit < 768 or headroom < 1024:
        rows = 128
    return dict(memory_limit_mb=limit, batch_rows=rows, batch_bytes=1024 * 1024,
                capacity_wait_seconds=max(0., min(900., float(
                    options.get('training_recorder_capacity_wait_seconds', 300)))))


def preparation_memory():
    from training_process import _host_memory_mb
    memory = _host_memory_mb()
    # /proc/meminfo can describe the host even when the add-on has a cgroup limit.
    for limit_path, used_path in (
        ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
        ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes'),
    ):
        try:
            limit = int(Path(limit_path).read_text().strip()) / (1024 * 1024)
            used = int(Path(used_path).read_text().strip()) / (1024 * 1024)
        except (OSError, ValueError):
            continue
        if limit <= 0:
            continue
        memory['total_mb'] = min(memory.get('total_mb') or limit, limit)
        memory['available_mb'] = min(memory.get('available_mb') or max(0., limit-used),
                                     max(0., limit-used))
    return memory

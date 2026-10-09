"""Narrow busy-wait policy for batches which already retain data on failure."""
from contextlib import nullcontext


def background_sqlite(store):
    scope = getattr(store, "background_sqlite", None)
    return scope() if callable(scope) else nullcontext()

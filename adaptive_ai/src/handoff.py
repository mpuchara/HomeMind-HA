"""Generic transactional handoff helper with persistent journal."""


class HandoffError(RuntimeError):
    def __init__(self, cause, changed, restored, failed):
        super().__init__(str(cause))
        self.cause = cause
        self.changed = list(changed or [])
        self.restored = list(restored or [])
        self.failed = list(failed or [])


def restore_items(items, restore_one):
    restored, failed = [], []
    for item in reversed(list(items or [])):
        try:
            restore_one(item)
            restored.append(item)
        except Exception as exc:
            failed.append({"item": item, "error": f"{type(exc).__name__}: {exc}"})
    return restored, failed


def acquire_transaction(items, disable_one, restore_one, checkpoint, confirm):
    """Apply side effects one by one and checkpoint after every successful step."""
    changed = []
    try:
        for item in items:
            disable_one(item)
            changed.append(item)
            checkpoint(changed)
        confirm(changed)
        checkpoint(changed)
        return changed
    except Exception as exc:
        restored, failed = restore_items(changed, restore_one)
        raise HandoffError(exc, changed, restored, failed) from exc

"""Generic transactional handoff helper with persistent journal."""


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
    """Disable items one by one, checkpointing after every side effect.

    If any step fails, all completed side effects are rolled back. The caller can persist
    failed rollback items for startup recovery.
    """
    changed = []
    try:
        for item in items:
            disable_one(item)
            changed.append(item)
            checkpoint(changed)
        confirm(changed)
        checkpoint(changed)
        return changed, []
    except Exception:
        _, failed = restore_items(changed, restore_one)
        raise

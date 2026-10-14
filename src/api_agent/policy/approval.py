"""Cooperative CLI approval без бесконечного блокирования Run на stdin."""

import queue
import threading
import time


def read_approval(proposal, *, reader, cancel, timeout):
    """Только точное yes подтверждает действие; EOF/timeout/cancel означают отказ.

    Daemon reader не выполняет эффектов и не удерживает процесс после остановки.
    Поток нужен потому, что обычный select не читает Windows console stdin.
    """
    response = {
        "action_hash": proposal["action_hash"],
        "config_version": proposal["config_version"],
        "approved": False,
    }
    if cancel.is_set() or timeout <= 0:
        return response
    replies = queue.Queue(maxsize=1)

    def collect():
        try:
            value = reader()
        except Exception:
            value = None
        replies.put(value)

    threading.Thread(target=collect, daemon=True).start()
    deadline = time.monotonic() + timeout
    while not cancel.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            value = replies.get(timeout=min(0.05, left))
        except queue.Empty:
            continue
        response["approved"] = (
            isinstance(value, str)
            and value.strip() == "yes"
            and not cancel.is_set()
            and time.monotonic() < deadline
        )
        break
    return response

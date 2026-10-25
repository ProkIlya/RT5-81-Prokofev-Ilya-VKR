"""Общие бюджеты; основной исполнитель находится в workspace.runtime."""
from .execution.contracts import Limits

__all__ = ["Limits"]

def StartAgentRun(*args, **kwargs):
    """Совместимость импортов старых test helpers, без исполнения и fallback."""
    raise RuntimeError("structured_runtime_retired_use_workspace")

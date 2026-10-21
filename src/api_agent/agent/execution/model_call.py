"""Вызов модели: deadline, безопасные timing/error и остановка без replay."""
from copy import deepcopy

from ...bounded_json import numeric_metadata
from ..model import ModelError, ModelTimeout
from ..resilience import tool_result


def request_model(self, run, model, session, payload, step, budget, emit, guard, stop):
    try:
        if hasattr(model, 'set_deadline'):
            model.set_deadline(session.deadline)
        response = model(deepcopy(payload))
    except Exception as error:
        budget.unknown = True
        run["resources"] = budget.snapshot()
        code = "model_error"
        if isinstance(error, ModelTimeout):
            code = "model_timeout"
        elif isinstance(error, ModelError) and error.code in {
            "model_http_error",
            "model_response_limit",
            "model_transport_error",
            "model_malformed_stream",
            "model_incomplete_stream",
            "model_generation_limit",
        }:
            code = error.code
        if isinstance(error, (ModelError, ModelTimeout)):
            try:
                step["timing"] = numeric_metadata(
                    error.timing, ("ttft_seconds", "wall_seconds")
                )
                if isinstance(error, ModelError) and error.finish_reason in (
                    "length",
                    "stop",
                    "tool_calls",
                ):
                    step["finish_reason"] = error.finish_reason
            except (ValueError, TypeError):
                pass
        step["observation"] = tool_result(
            error_code="upstream_error",
            hint="Модель не вернула полное действие; изучите причину остановки.",
        )
        emit("model_error", {"code": code})
        if guard():
            stop(code, "failed")
        return False, None
    if not guard():
        budget.unknown = True
        run["resources"] = budget.snapshot()
        return False, None
    return True, response

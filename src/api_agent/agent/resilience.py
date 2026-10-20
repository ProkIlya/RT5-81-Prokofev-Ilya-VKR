"""Ограниченные ресурсы, безопасный журнал и рабочее окно одного Run.

Полная история хранится отдельно от payload модели. Сжатие и восстановление
между процессами здесь не реализуются. Журнал сохраняется до HTTP; отказ
сохранения не разрешает продолжать побочные действия.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from ..bounded_json import validate_tree
from ..validation import fingerprint

ERROR_CODES = {
    "validation",
    "unknown_tool",
    "permission_denied",
    "not_found",
    "conflict",
    "rate_limited",
    "timeout",
    "too_large",
    "upstream_error",
    "invariant_violated",
    "needs_confirmation",
}


class BudgetViolation(ValueError):
    """Безопасная причина отказа preflight/accounting без исходного prompt."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class EmptyDecision(ValueError):
    """Полный ответ LLM без native action; отдельный от malformed счётчик."""


class JsonlJournal:
    """Append-only JSONL; fsync завершается до передачи управления к HTTP.

    Путь выбирает приложение, имя файла — проверенный run_id, не модель.
    Raw тела, исключения и prompt в этот журнал не принимаются.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, event):
        import uuid

        run_id = event["run_id"]
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("invalid_run_id")
        validate_tree(event, max_bytes=131072)
        data = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8"
        )
        with (self.directory / (run_id + ".jsonl")).open("ab") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())


def tool_result(
    data=None,
    *,
    error_code=None,
    hint=None,
    evidence_ids=(),
    provenance=None,
    bytes_total=0,
    omitted_fields=(),
    truncated=False
):
    """Закрытый результат dispatch; подсказка безопасна и ограничена.

    Отсутствие response.json в проекции явно отмечается. Это не transport
    truncation: полный ограниченный ответ проверяется внутренним validator.
    """
    if error_code is not None and error_code not in ERROR_CODES:
        raise ValueError("unknown_error_code")
    return {
        "status": "error" if error_code else "ok",
        "data": data if not error_code else None,
        "error": (
            None
            if not error_code
            else {
                "error_code": error_code,
                "message": "Инструмент не выполнил действие.",
                "hint": (
                    hint
                    or "Используйте разрешённый инструмент с корректными аргументами."
                )[:200],
                "retryable": False,
                "retry_after": None,
            }
        ),
        "evidence_ids": list(evidence_ids),
        "provenance": provenance or {},
        "bytes_total": bytes_total,
        "bytes_returned": (
            len(json.dumps(data, ensure_ascii=False).encode())
            if data is not None
            else 0
        ),
        "truncated": truncated,
        "omitted_fields": list(omitted_fields),
        "summary_only": bool(omitted_fields),
    }


def build_window(history):
    """Создать независимую копию безопасной истории без потери native pairs.

    Короткий S05 не требует сжатия. При нехватке окна preflight останавливает
    Run; молчаливая обрезка оснований или tool_call_id запрещена.
    """
    return deepcopy(history)


class TokenBudget:
    """Резервировать templated input + максимальную generation до LLM.

    Счётчик — доверенная граница runtime tokenizer, не оценка bytes/4.
    При неизвестном actual usage резерв не освобождается; измеренные totals
    помечаются unknown, а консервативная верхняя граница сохраняется отдельно.
    """

    def __init__(self, limits, counter):
        self.limits, self.counter = limits, counter
        self.upper = 0
        self.input = self.output = 0
        self.calls = 0
        self.unknown = False
        self.pending = False
        self.last_input = None

    def snapshot(self):
        known = self.calls > 0 and not self.unknown and not self.pending
        return {
            "usage_status": "measured" if known else "unknown",
            "input_tokens": self.input if known else None,
            "output_tokens": self.output if known else None,
            "total_tokens": self.input + self.output if known else None,
            "tokens_upper_bound": self.upper,
            "max_tokens_total": self.limits.max_tokens_total,
            "provider_fee": {
                "applicability": "N/A",
                "amount": 0,
                "basis": "Локальная GGUF-модель; платный провайдер не используется. Время и аппаратные ресурсы учитываются отдельно.",
            },
        }

    def reserve(self, payload):
        if self.counter is None:
            raise BudgetViolation("token_preflight_error")
        try:
            count = self.counter(deepcopy(payload))
        except Exception:
            raise BudgetViolation("token_preflight_error") from None
        if type(count) is not int or count < 1:
            raise BudgetViolation("token_preflight_error")
        reserve = count + payload["max_tokens"]
        if reserve > self.limits.max_context_tokens:
            raise BudgetViolation("context_limit")
        if self.upper + reserve > self.limits.max_tokens_total:
            raise BudgetViolation("token_limit")
        self.pending = True
        self.last_input = count
        self.upper += reserve
        self.calls += 1
        return count, reserve

    def release_unsent(self, reserved):
        """Освободить только неотправленный резерв для пересборки окна.

        Уже выполненные генерации остаются в cumulative budget. Этот метод
        вызывается до model_requested, никогда после отправки payload.
        """
        self.upper -= reserved
        self.calls -= 1
        self.pending = False

    def settle(self, usage, max_output):
        if not usage:
            self.pending = False
            self.unknown = True
            return
        if (
            set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(type(v) is not int or v < 0 for v in usage.values())
            or usage["prompt_tokens"] != self.last_input
            or usage["completion_tokens"] > max_output
            or usage["total_tokens"]
            != usage["prompt_tokens"] + usage["completion_tokens"]
        ):
            self.unknown = True
            raise BudgetViolation("token_accounting_error")
        self.pending = False
        self.upper -= max_output - usage["completion_tokens"]
        self.input += usage["prompt_tokens"]
        self.output += usage["completion_tokens"]


def stop_category(run):
    """Нормализовать остановку, сохранив подробную исходную причину."""
    reason = run["stop_reason"]
    if reason == "finished":
        return (
            "done"
            if run["status"] == "completed"
            and (run.get("validation") or {}).get("task_postcondition")
            else "done_unverified"
        )
    return {
        "step_limit": "budget_steps",
        "time_limit": "budget_time",
        "token_limit": "budget_tokens",
        "repeated_action": "repeated_action",
        "no_progress": "no_progress",
        "cancelled": "cancelled",
        "request_limit": "request_limit",
        "context_limit": "prompt_limit",
        "prompt_limit": "prompt_limit",
        "malformed_limit": "malformed_limit",
        "empty_limit": "empty_limit",
        "invalid_expectation": "needs_human",
        "clarification_required": "needs_human",
        "contract_redaction_conflict": "needs_human",
        'uncertain_write': 'needs_human',
        'unresolved_uncertain_write': 'needs_human',
        'target_not_applicable': 'needs_human',
        'precondition_not_verified': 'needs_human',
        'unsupported_goal_capability': 'needs_human',
        'stale_auth_epoch': 'needs_human',
    }.get(reason, "fatal")


def event_record(run, kind, details):
    """Обязательные версии и ресурсы каждой записи; время хранится в UTC."""
    step = run["steps"][-1] if run["steps"] else {}
    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run["run_id"],
        "sequence": len(run["events"]) + 1,
        "step": step.get("index", 0),
        "kind": kind,
        "action_name": (step.get("action") or {}).get("name"),
        "arguments_hash": fingerprint((step.get("action") or {}).get("arguments")),
        "decision_kind": step.get("decision_kind"),
        "model_version": run["model"]["name"],
        "prompt_version": "validation-loop-v1",
        "tools_version": run["tools_hash"],
        "policy_version": "loopback-get-v1",
        "spec_hash": run["spec_hash"],
        "window_hash": step.get("window_hash"),
        "tokens": deepcopy(run["resources"]),
        "budget_before": deepcopy(step.get("budget_before")),
        "budget_after": deepcopy(run["resources"]),
        "model_duration": (step.get("timing") or {}).get("wall_seconds"),
        "tool_duration": step.get("tool_duration"),
        "stop_reason": run["stop_reason"],
        "details": deepcopy(details),
    }

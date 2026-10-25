"""Общие бюджеты, схемы и проверка извлечения цепочек."""
from dataclasses import dataclass
from copy import deepcopy
import math
import re
from ...transport.http import PolicyDenied

def _exact_registry(registry, variants):
    """Разделить две формы выполнения на плоские схемы для native runtime.

    MiniCPM/runtime с union выдавал пустые аргументы. Два точных инструмента
    адаптируются к тому же dispatcher и не добавляют HTTP-полномочий.
    """
    execution = registry[0]["function"]["parameters"]
    executions = []
    for index, (operation, fields) in enumerate(variants):
        properties = {key: deepcopy(execution["properties"][key]) for key in fields}
        properties["operation_id"]["enum"] = [operation]
        description = (
            f"Execute {operation}. Credentials are supplied internally."
            if len(fields) == 1
            else (
                f"Create {operation} with body name A or B."
                if "body" in fields
                else f"Read {operation}: copy data.{fields[-1]} EXACTLY, not exchange_id."
            )
        )
        executions.append(
            {
                "type": "function",
                "function": {
                    "name": "execute_operation" if index == 0 else "execute_read",
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(fields),
                        "additionalProperties": False,
                    },
                },
            }
        )
    finish = registry[-1]["function"]["parameters"]["properties"]
    finish["summary"].update(minLength=1, maxLength=2000, pattern=r"^\s*\S[\s\S]*$")
    finish["evidence_ids"].update(minItems=1, maxItems=4)
    return executions + registry[1:]


def _validate_extraction(catalog, operation, status, path, kind, *, secret=False):
    """До Run сверяем доверенное правило извлечения с исходной схемой ответа.

    Объектный сегмент переводится в properties, индекс массива — в items.
    Секретные литералы в enum нельзя публиковать через inspect_operation.
    """
    pointer = f"/responses/{status}/content/application~1json/schema"
    try:
        for segment in path.lstrip("/").split("/"):
            parent = catalog.resolve_basis(operation, pointer, catalog.operation_hashes[operation])
            if parent.get('type') == 'array':
                if not re.fullmatch(r'0|[1-9][0-9]*', segment): raise ValueError('invalid_array_index')
                pointer += '/items'
            else:
                pointer += '/properties/' + segment
        schema = catalog.resolve_basis(
            operation, pointer, catalog.operation_hashes[operation]
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("undeclared_extraction_path") from error
    if schema.get("type") != kind or (secret and "enum" in schema):
        raise ValueError("invalid_extraction_schema")


class RequestLimitExceeded(PolicyDenied):
    """Исчерпание ресурса останавливает Run без коррекции аргументов модели."""

    stop_reason = "request_limit"


@dataclass(frozen=True)
class ChainLimits:
    """Конечные пользовательские бюджеты; контекст runtime не увеличивается."""

    max_output_tokens: int = 512
    max_steps: int = 24
    max_requests: int = 10
    max_seconds: float = 180
    malformed_retries: int = 1
    max_prompt_bytes: int = 16000
    max_tokens_total: int = 8192
    max_context_tokens: int = 2048
    max_empty_steps: int = 2
    max_repeats: int = 3

    def __post_init__(self):
        if (
            any(
                type(getattr(self, k)) is not int or getattr(self, k) < 1
                for k in (
                    "max_output_tokens",
                    "max_steps",
                    "max_requests",
                    "max_prompt_bytes",
                    "max_tokens_total",
                    "max_context_tokens",
                    "max_empty_steps",
                    "max_repeats",
                )
            )
            or type(self.max_seconds) not in (int, float)
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
            or type(self.malformed_retries) is not int
            or self.malformed_retries < 0
            or self.max_output_tokens >= self.max_context_tokens
        ):
            raise ValueError("invalid_chain_budget")

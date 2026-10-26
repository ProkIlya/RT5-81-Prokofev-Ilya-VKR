"""Конечные бюджеты и typed ошибки shared code execution kernel."""
from dataclasses import dataclass
import math
import re
from api_agent.transport.http import PolicyDenied

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


@dataclass(frozen=True)
class CatalogLimits(ChainLimits):
    """Две correction-попытки общего registry; каждый ход расходует тот же budget."""

    max_requests: int = 20
    max_context_tokens: int = 8192
    max_output_tokens: int = 1024
    max_tokens_total: int = 65536
    max_prompt_bytes: int = 65536
    malformed_retries: int = 2


class CatalogArgumentsError(ValueError):
    """Подсказка формы использует schema, никогда отклонённые значения модели."""

    def __init__(self, code, hint):
        self.repair_hint = hint
        super().__init__(code)

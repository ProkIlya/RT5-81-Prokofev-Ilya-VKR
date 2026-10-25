"""Бюджеты каталога и безопасные типы решений."""
from dataclasses import dataclass
from ...agent.chains import ChainLimits

@dataclass(frozen=True)
class CatalogLimits(ChainLimits):
    """Две correction-попытки общего registry; каждый ход расходует тот же budget."""

    max_requests: int = 20
    max_context_tokens: int = 8192
    max_output_tokens: int = 1024
    max_tokens_total: int = 32768
    max_prompt_bytes: int = 65536
    malformed_retries: int = 2


class CatalogArgumentsError(ValueError):
    """Подсказка формы использует schema, никогда отклонённые значения модели."""

    def __init__(self, code, hint):
        self.repair_hint = hint
        super().__init__(code)


@dataclass(frozen=True)
class PhaseDecision:
    """Один согласованный результат: опубликованные tools и choice из них."""

    tools: list
    tool_choice: object

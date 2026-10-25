"""Формы инструментов, бюджеты и ошибки structured loop."""
from dataclasses import dataclass
import math


class ToolArgumentsError(ValueError):
    """Ошибка формы с безопасной подсказкой из доверенного tool registry.

    Значения аргументов не включаются: malformed call может содержать секрет.
    """

    def __init__(self, hint):
        super().__init__("invalid_tool_arguments")
        self.repair_hint = hint


@dataclass(frozen=True)
class Limits:
    """Конечные бюджеты; попытки LLM и HTTP учитываются до внешнего действия."""

    max_steps: int = 8
    max_requests: int = 1
    max_seconds: float = 180
    malformed_retries: int = 1
    max_prompt_bytes: int = 12000
    max_tokens_total: int = 8192
    max_context_tokens: int = 2048
    max_empty_steps: int = 2
    max_repeats: int = 3

    def __post_init__(self):
        for value in (
            self.max_steps,
            self.max_requests,
            self.max_prompt_bytes,
            self.max_tokens_total,
            self.max_context_tokens,
            self.max_empty_steps,
            self.max_repeats,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("invalid budget")
        if type(self.malformed_retries) is not int or self.malformed_retries < 0:
            raise ValueError("invalid retry budget")
        if (
            type(self.max_seconds) not in (int, float)
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("invalid time budget")
        if self.max_requests != 1:
            raise ValueError("S04 transport supports one request")

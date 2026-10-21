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


def tools(workflow=None, catalog=None):
    if workflow is not None:
        return workflow.registry(catalog)
    """Закрытый стабильный реестр: аргументы модели никогда не содержат URL."""
    operation = {
        "type": "object",
        "properties": {
            "operation_id": {
                "type": "string",
                "enum": ["getPet"],
                "minLength": 6,
                "maxLength": 6,
                "description": "Единственная разрешённая операция GET /pets/1.",
            }
        },
        "required": ["operation_id"],
        "additionalProperties": False,
    }
    finish = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2000,
                "description": "Краткое пояснение; результаты проверки назначает приложение.",
            },
            "evidence_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "ID реального обмена текущего Run.",
                },
                "minItems": 1,
                "maxItems": 1,
            },
        },
        "required": ["summary", "evidence_ids"],
        "additionalProperties": False,
    }
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        }
        for name, description, schema in (
            (
                "execute_operation",
                'Выполнить единственный GET /pets/1 и получить обмен с результатами проверки.\nПрименять для проверки разрешённого питомца; другой адрес или операция запрещены.\nНе более одного HTTP на Run. Пример: {"operation_id":"getPet"}.',
                operation,
            ),
            (
                "finish_run",
                'Сохранить итог по одному существующему обмену текущего Run.\nПрименять после HTTP; без evidence завершение отклоняется.\nsummary до 2000 символов, evidence_ids содержит один ID. Пример: {"summary":"Проверка выполнена","evidence_ids":["ID из observation"]}.',
                finish,
            ),
            (
                "inspect_operation",
                'Прочитать разрешённую операцию и объявленные статусы ответа без HTTP.\nПрименять перед проверкой или после неожиданного ответа; не для смены цели.\nНе более трёх одинаковых вызовов. Пример: {"operation_id":"getPet"}.',
                operation,
            ),
        )
    ]

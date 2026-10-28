"""Бюджет единственного code loop; прежние native loop helpers архивированы."""
from copy import deepcopy

GENERATION_REPAIR_HINT = (
    'The previous model reply exceeded its output limit and was rejected. No action from that reply ran. '
    'Return concise executable code or tool arguments, omit comments and progress prints. '
    'You may split remaining work across calls. Keep completed requests; do not repeat them.'
)


class BudgetViolation(ValueError):
    """Безопасная причина отказа preflight/accounting без исходного prompt."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class EmptyDecision(ValueError):
    """Полный ответ LLM без native action; отдельный от malformed счётчик."""


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

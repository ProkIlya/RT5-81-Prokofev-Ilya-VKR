"""Адаптивный цикл с детерминированными правами и наблюдаемой остановкой.

LLM не создаёт Run, не выбирает target и не назначает verdict. SQLiteHistory
обеспечивает дедупликацию после рестарта; без него действует in-memory режим.
"""

from copy import deepcopy
import hashlib
import json
import threading
import time

from ..transport.http import loopback_origin
from ..openapi.parser import canonical
from ..bounded_json import validate_tree
from ..validation import validate_exchange, schema_issues
from .resilience import EmptyDecision
from .execution.contracts import Limits, tools, ToolArgumentsError
from .execution.command import RunCommand
from .execution.lifecycle import RunLifecycle

class StartAgentRun(RunCommand, RunLifecycle):
    """Единственный владелец Run; повтор команды не запускает ещё один loop.

    Один lock сериализует все команды экземпляра, включая конкурентную доставку.
    Модель видит только redacted копии. trace_sink получает безопасный снимок
    перед сетью и следующим ходом; ошибка сохранения прекращает новые действия.
    """
    def __init__(
        self,
        target,
        catalog,
        *,
        limits=None,
        workflow=None,
        trace_sink=None,
        token_counter=None,
        event_sink=None,
        secrets=(),
        clock=time.monotonic,
        history_store=None,
        environment="default",
        model_name="MiniCPM5-2B-Q4_K_M",
        summary_reviewer=None,
    ):
        self.target, _ = loopback_origin(target)
        self.catalog = catalog
        self.workflow = workflow
        # Private credentials остаются в памяти приложения; сравнение не
        # публикует ни значения, ни перебираемый hash низкоэнтропийного пароля.
        self._accepted_auth = deepcopy(getattr(workflow, 'auth', None))
        self.initial_operation = workflow.create if workflow else "getPet"
        if workflow is not None:
            workflow.validate_catalog(catalog)
        operation = catalog.operations.get("getPet")
        if workflow is None and (
            not operation
            or operation["method"] != "GET"
            or operation["path"] != "/pets/1"
            or operation["parameters"]
            or operation["security"]
            or "requestBody" in operation
        ):
            raise ValueError("unsupported executable operation")
        # Каталог не расширяет полномочия транспорта; остальные операции не выдаются tools.
        self.operation_hash = catalog.operation_hashes[self.initial_operation]
        if workflow is None:
            catalog.resolve_basis("getPet", "/responses/200", self.operation_hash)
        from .chains import ChainLimits

        self.limits = limits or (
            workflow.default_limits()
            if workflow and hasattr(workflow, "default_limits")
            else ChainLimits() if workflow else Limits()
        )
        self.trace_sink = trace_sink or (lambda run: None)
        # Library-вызовы сохраняют in-memory события в Run. Production CLI
        # дополнительно обязательно передаёт JsonlJournal; это не no-op sink.
        self.token_counter, self.event_sink = token_counter, event_sink
        if not isinstance(secrets, (tuple, list)) or any(
            not isinstance(s, str) or not s for s in secrets
        ):
            raise ValueError("invalid secret configuration")
        secrets = tuple(secrets) + (
            workflow.secrets() if workflow and hasattr(workflow, "secrets") else ()
        )
        self.secrets = tuple(sorted(secrets, key=len, reverse=True))
        self._active_state = None
        self.clock = clock
        self.history_store = history_store
        self.environment = environment
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 200:
            raise ValueError("invalid_model_identity")
        self.model_name = model_name
        self.summary_reviewer = summary_reviewer
        self._runs = {}
        self._lock = threading.Lock()

    def safe(self, value, *, _public_metadata=False):
        """Маскировать доверенно настроенные secrets до любого наблюдаемого выхода.

        Это конечная классификация приложения, не обещание распознать неизвестный
        секрет. Необработанные exceptions и ответы модели в evidence не записываются.
        """
        # Run содержит несколько уже ограниченных фрагментов и обёртки trace.
        # Его конечный бюджет шире бюджета одного ответа, глубина всё ещё
        # значительно меньше стека Python. Недоверенный model response
        # проверяется отдельно до попадания сюда с более строгими пределами.
        validate_tree(value, max_depth=96, max_nodes=100000, max_bytes=4194304)

        def redact(item):
            if isinstance(item, str):
                for secret in self.secrets:
                    item = item.replace(secret, "[REDACTED]")
                return item
            if isinstance(item, dict):
                return {redact(k): redact(v) for k, v in item.items()}
            if isinstance(item, list):
                return [redact(v) for v in item]
            return item

        # Сначала пути по исходным именам ключей: статический secret может
        # совпасть с именем token и переименовать его до структурной редакции.
        # Строковое маскирование после этого уже не может открыть ветвь.
        result = (
            (self._active_state.redact_public_metadata(value) if _public_metadata
             else self._active_state.redact(value))
            if self._active_state else value
        )
        return redact(result)

    def _safe_run(self, run):
        """Явная граница Run DTO: только contract является публичным metadata.

        Не ищем имя contract рекурсивно: ответ API не может объявить себя
        доверенным снимком. Маскирование literal не отключается; изменение
        основания по-прежнему отклоняет immutable guard хранилища.
        """
        metadata = {'contract', 'admission_manifest', 'policy_config', 'run_catalog'}
        public = self.safe({k: v for k, v in run.items() if k not in metadata})
        for key in metadata & run.keys():
            public[key] = self.safe(run[key], _public_metadata=True)
        return public

    def _tool_content(self, result):
        """Сначала маскировать DTO, затем применить формат профиля workflow."""
        projected = (
            self.workflow.model_result(result)
            if self.workflow and hasattr(self.workflow, "model_result")
            else result
        )
        public = self.safe(projected)
        if self.workflow and hasattr(self.workflow, "model_content"):
            return self.workflow.model_content(public)
        return json.dumps(public, ensure_ascii=False)

    def _decision(self, response):
        """Не доверять JSON-схеме runtime: повторно проверить форму и семантику."""
        if len(canonical(response)) > 1048576:
            raise ValueError("response limit")
        choices = response["choices"]
        if isinstance(choices, list) and not choices:
            raise EmptyDecision()
        if (
            isinstance(choices, list)
            and len(choices) == 1
            and isinstance(choices[0], dict)
        ):
            message = choices[0].get("message")
            if isinstance(message, dict) and (
                "tool_calls" not in message or message["tool_calls"] == []
            ):
                raise EmptyDecision()
        calls = choices[0]["message"]["tool_calls"]
        call = calls[0]
        if (
            len(choices) != 1
            or len(calls) != 1
            or call["type"] != "function"
            or not isinstance(call["id"], str)
            or not call["id"]
            or len(call["id"]) > 200
        ):
            raise ValueError("invalid native call")
        name = call["function"]["name"]
        raw = call["function"]["arguments"]
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ValueError("invalid arguments")
        args = json.loads(raw)
        validate_tree(args, max_bytes=16384)
        if self.workflow:
            published = next(
                (
                    tool["function"]["parameters"]
                    for tool in tools(self.workflow, self.catalog)
                    if tool["function"]["name"] == name
                ),
                None,
            )
            if published is None:
                raise ValueError("unknown_tool")
            if schema_issues(args, published):
                required = ", ".join(published.get("required", []))
                operations = (
                    published.get("properties", {})
                    .get("operation_id", {})
                    .get("enum", [])
                )
                hint = f"Invalid arguments for {name}. Required fields: {required}. No extra fields."
                if operations:
                    hint += " Allowed operation_id: " + ", ".join(operations) + "."
                raise ToolArgumentsError(hint)
            self.workflow.decision(name, args)
        elif name in ("execute_operation", "inspect_operation"):
            if args != {"operation_id": "getPet"}:
                raise ValueError("operation denied")
        elif name == "finish_run":
            if (
                not isinstance(args, dict)
                or set(args) != {"summary", "evidence_ids"}
                or not isinstance(args["summary"], str)
                or not args["summary"].strip()
                or len(args["summary"]) > 2000
                or not isinstance(args["evidence_ids"], list)
                or len(args["evidence_ids"]) != 1
                or not isinstance(args["evidence_ids"][0], str)
                or not 1 <= len(args["evidence_ids"][0]) <= 200
            ):
                raise ValueError("invalid finish")
        else:
            raise ValueError("unknown tool")
        return call, {"name": name, "arguments": args}

    def _drive_loop(self, run, model, cancel, session, started, prepared, budget):
        from .execution.dispatch import drive_loop
        return drive_loop(self, run, model, cancel, session, started, prepared, budget)

"""Создание Run, дедупликация команды и durable финализация."""
from copy import deepcopy
from contextlib import nullcontext
import threading
import uuid

from ...validation import prepare_expectations, fingerprint, report
from ..resilience import TokenBudget

from .contracts import tools

class RunCommand:
    def start(
        self,
        project_id,
        session_id,
        message_id,
        goal,
        model,
        *,
        cancel=None,
        conditions=None,
        checks_only=False,
        parent_run_id=None,
    ):
        """Принять команду приложения; идентичный message_id возвращает снимок Run.

        Подмена Project/Session для того же сообщения отклоняется. Даже неуспешный
        Run не повторяется автоматически: явный retest требует нового сообщения.
        """
        if type(checks_only) is not bool:
            raise ValueError("invalid_goal_scope")
        for value in (project_id, session_id, message_id, goal):
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError("invalid command")
        lease = self.history_store.execution() if self.history_store else nullcontext()
        with self._lock, lease:
            if self.history_store:
                previous = self.history_store.lookup(project_id, session_id, message_id)
                if previous is not None:
                    return previous
            if message_id in self._runs:
                entry = self._runs[message_id]
                if entry["scope"] != (project_id, session_id):
                    raise ValueError("message scope mismatch")
                return deepcopy(entry["run"])
            run = {
                "run_id": str(uuid.uuid4()),
                "project_id": project_id,
                "session_id": session_id,
                "message_id": message_id,
                "goal": goal,
                "goal_coverage": getattr(self.workflow, "goal_coverage", "partial"),
                "target": self.target,
                "contract": self.catalog.snapshot,
                "spec_hash": self.catalog.spec_hash,
                "operation_hash": self.operation_hash,
                "model": {
                    "name": self.model_name,
                    "sampling": {
                        "temperature": 0.0,
                        "top_p": 0.95,
                        "min_p": 0.0,
                        "max_tokens": (
                            self.limits.max_output_tokens if self.workflow else 384
                        ),
                    },
                },
                "limits": vars(self.limits),
                "status": "partial",
                "stop_reason": None,
                "steps": [],
                "exchanges": [],
                "expectations": [],
                "requests": 0,
                "model_calls": 0,
                "malformed_calls": 0,
                "validation": None,
                "empty_steps": 0,
                "empty_consecutive": 0,
                "history": [],
                "events": [],
                "warnings": [],
                "tools_hash": fingerprint(tools(self.workflow, self.catalog)),
                "resources": TokenBudget(self.limits, None).snapshot(),
                "goal_hash": fingerprint(self.safe(goal)),
                "final": None,
                **({"parent_run_id": parent_run_id} if parent_run_id else {}),
            }
            # Один режим генерации для всех циклов: скрытый thinking иначе
            # расходует бюджет ответа до появления native tool call.
            run["model"]["request_options"] = {
                "parallel_tool_calls": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if self.workflow and hasattr(self.workflow, 'runtime_manifest'):
                run['admission_manifest'] = self.workflow.runtime_manifest()
            # Снимок и hashes должны описывать одно доступное основание. Маска
            # literal enum, имени поля или pointer меняет контракт, поэтому
            # redaction не может служить его каноническим преобразованием.
            # Проверяем весь поддержанный снимок и идентичность basis ДО записи
            # и любых действий. Аннотации уже удалены профилем S03 до hashes.
            identity = {
                "contract": run["contract"],
                "spec_hash": run["spec_hash"],
                "operation_hash": run["operation_hash"],
                "basis": {
                    "source": "CONTRACT",
                    "operation_id": self.initial_operation,
                    "pointer": (
                        "/responses/200"
                        if not self.workflow or hasattr(self.workflow, "secrets")
                        else "/responses/201"
                    ),
                },
                **({'admission_manifest': run['admission_manifest']} if 'admission_manifest' in run else {}),
            }
            if self.safe(identity) != identity:
                # Нельзя сохранять секретное ограничение или объявлять маску
                # эквивалентным enum. У отказанного Run нет contract/hashes и
                # CONTRACT expectation; причина безопасна и не содержит значения.
                run.update(
                    contract=None,
                    spec_hash=None,
                    operation_hash=None,
                    status="blocked",
                    stop_reason="contract_redaction_conflict",
                )
            if run['stop_reason'] is None and getattr(self.workflow, 'feasibility_stop', None):
                run.update(status='blocked', stop_reason=self.workflow.feasibility_stop)
            if run['stop_reason'] is None and len(getattr(self.workflow, 'occurrences', [])) > self.limits.max_requests:
                run.update(status='blocked', stop_reason='request_limit')
            # Регистрация раньше запуска исключает повтор HTTP после ошибки trace.
            # Исходные trusted IDs — приватная authority, только в памяти под
            # lock. Маскированный DTO не определяет принадлежность команды:
            # разные исходные ID могут иметь одинаковое отображение. Scope
            # никогда не передаётся в sink, prompt, исключения или public return.
            entry = {"scope": (project_id, session_id), "run": self._safe_run(run)}
            # Узкий S05 распознаёт только известную цель проверки getPet.
            # Явные нормализованные conditions или checks_only — доверенная
            # команда UI/application, не полномочие модели переименовать цель.
            known_goals = {
                "check pet",
                "check getpet",
                "проверь питомца",
                "проверь контракт getpet",
                "проверить контракт getpet",
                "check the test pet and report the actual observation",
            }
            normalized_goal = goal.strip().rstrip(".").casefold()
            if run["stop_reason"] is None and not (
                (
                    self.workflow.goal_allowed(goal)
                    if self.workflow
                    else checks_only
                    or conditions is not None
                    or normalized_goal in known_goals
                )
            ):
                run.update(status="blocked", stop_reason="clarification_required")
            if self.workflow and (conditions is not None or checks_only):
                run.update(status="blocked", stop_reason="invalid_expectation")
            run["completion_scope"] = "supported_checks_only"
            if run["stop_reason"] is None:
                try:
                    prepared = (
                        self.workflow.prepared(self.catalog, message_id, goal)
                        if self.workflow
                        else prepare_expectations(
                            self.catalog, conditions, message_id, goal
                        )
                    )
                    # Исходные значения условий живут только в памяти до проверки;
                    # безопасный снимок фиксируется до первого сетевого действия.
                    run["expectations"] = self.safe(
                        [e for group in prepared.values() for e in group]
                        if self.workflow
                        else prepared
                    )
                except (ValueError, KeyError, TypeError):
                    run.update(status="blocked", stop_reason="invalid_expectation")
                    prepared = []
            else:
                prepared = []
            if self.history_store:
                configuration = (
                    self.workflow.config.snapshot
                    if self.workflow and hasattr(self.workflow, "config")
                    else {"version": 1, "spec_hash": self.catalog.spec_hash,
                          "operations": {self.initial_operation: {"operation_hash": self.operation_hash, "risk": "A"}}}
                )
                if configuration.get("project_id", project_id) != project_id:
                    raise ValueError("foreign_project_configuration")
                metadata = {
                    "extraction": getattr(self.workflow, "extraction", {}),
                    "binding_rules": getattr(self.workflow, "binding_rules", {}),
                    "selection_scopes": getattr(self.workflow, "selection_scopes", {}),
                    "request_auth": getattr(self.workflow, "request_auth", {}),
                    "occurrences": getattr(self.workflow, "occurrences", []),
                    "admission_rules": getattr(self.workflow, "admission_rules", {}),
                    **({'admission_manifest': run['admission_manifest']} if 'admission_manifest' in run else {}),
                }
                auth = getattr(self.workflow, "auth", None)
                if auth:
                    metadata["auth"] = {k: auth[k] for k in ("login_operation", "token_path")}
                    metadata["auth"]["public_body_fields"] = list(auth.get("public_body_fields", []))
                if hasattr(self.workflow, 'storage_metadata'):
                    metadata = self.workflow.storage_metadata()
                previous = self.history_store.begin_run(
                    project_id, session_id, message_id, self._safe_run(run),
                    environment=self.environment,
                    configuration=self.safe(configuration), metadata=self.safe(metadata),
                )
                if previous is not None:
                    return previous
            # Команда считается принятой после атомарной durable регистрации.
            # Ошибка INSERT не должна оставлять ложный Run в in-memory dedup.
            self._runs[message_id] = entry
            result = self._loop(run, model, cancel or threading.Event(), prepared)
            if self.history_store:
                result["response_message_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "answer:" + result["run_id"]))
                try:
                    self.history_store.finalize(project_id, session_id, message_id, self._safe_run(result))
                except Exception:
                    if result.get('primary_safety_cause'):
                        result.update(status='failed', secondary_stop_reason='storage_error')
                    else:
                        result.update(status="failed", stop_reason="storage_error")
                    result["report"] = self.safe(report(result))
                else:
                    # Durable ответ уже создан: экспорт показывает ту же связь,
                    # не переписывая immutable finalized snapshot в SQLite.
                    self.trace_sink(self._safe_run(result))
            entry["run"] = self._safe_run(result)
            return deepcopy(entry["run"])
